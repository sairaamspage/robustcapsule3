import torch.nn as nn
import torch.nn.functional as F
import torch
from groupy.gconv.pytorch_gconv import P4MConvZ2, P4MConvP4M, P4ConvZ2, P4ConvP4
from groupy.gconv.pytorch_gconv.pooling import plane_group_spatial_max_pooling
import torch.nn.functional as F

class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self, in_planes, planes, stride=1):
        super(BasicBlock, self).__init__()
        self.conv1 = P4ConvP4(in_planes, planes, kernel_size=3, stride=stride, padding=1, bias=False)
        self.bn1 = nn.BatchNorm3d(planes)
        self.conv2 = P4ConvP4(planes, planes, kernel_size=3, stride=1, padding=1, bias=False)
        self.bn2 = nn.BatchNorm3d(planes)

        self.shortcut = nn.Sequential()
        if stride != 1 or in_planes != self.expansion*planes:
            self.shortcut = nn.Sequential(
                P4ConvP4(in_planes, self.expansion*planes, kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm3d(self.expansion*planes)
            )

    def forward(self, x):
        out = F.relu(self.bn1(self.conv1(x)))
        out = self.bn2(self.conv2(out))
        out += self.shortcut(x)
        out = F.relu(out)
        return out

class ResNetBlock(nn.Module):
    def __init__(self, cl_input_channels, cl_num_filters,
                 cl_stride):   
        super(ResNetBlock, self).__init__()
        self.in_planes = 64
        def _make_layer(block, planes, num_blocks, stride):
            strides = [stride] + [1]*(num_blocks-1)
            layers = []
            for stride in strides:
                layers.append(block(self.in_planes, planes, stride))
                self.in_planes = planes * block.expansion
            return nn.Sequential(*layers)
        
        self.pre_caps = nn.Sequential(
            P4ConvZ2(in_channels=cl_input_channels, 
                      out_channels=64, 
                      kernel_size=3, 
                      stride=1, 
                      padding=1, 
                      bias=False),
            nn.BatchNorm3d(64),
            nn.ReLU(),
            _make_layer(block=BasicBlock, planes=64, num_blocks=3, stride=1), # num_blocks=2 or 3
            _make_layer(block=BasicBlock, planes=cl_num_filters, num_blocks=4, stride=cl_stride), # num_blocks=2 or 4
        )
    def forward(self, x):
        out = self.pre_caps(x) # x is an image
        return out

def resnet_block():
    return ResNetBlock(BasicP4Block,[3,4]) 
                   
class PrimaryCapsules(nn.Module):
    def __init__(self,in_channels,num_capsules,out_dim,H=16,W=16):
        super(PrimaryCapsules,self).__init__()
        self.in_channels = in_channels
        self.num_capsules = num_capsules
        self.out_dim = out_dim
        self.H = H
        self.W = W
        self.preds = nn.Sequential(
                                   P4ConvP4(in_channels,num_capsules*out_dim,kernel_size=3,padding=1),
                                   nn.LayerNorm((num_capsules*out_dim,4,H,W)))

    def forward(self,x):
        primary_capsules = self.preds(x)
        primary_capsules = primary_capsules.view(-1,self.num_capsules,self.out_dim,4,self.H,self.W)
        return primary_capsules

class ConvolutionalCapsules(nn.Module):
    def __init__(self,in_caps,in_dim,out_caps,out_dim,kernel_size,stride=1,dilation=1,padding=0):
        super(ConvolutionalCapsules,self).__init__()
        self.in_caps = in_caps
        self.in_dim = in_dim
        self.out_caps = out_caps
        self.out_dim = out_dim
        self.preds = nn.Sequential(
                                   P4ConvP4(in_dim,out_caps*out_dim,kernel_size=kernel_size,stride=stride,padding=padding),
                                  )
        self.layer_norm = nn.LayerNorm(out_dim)
     
    def forward(self,in_capsules,k=10,ITER=2):
        batch_size, _, _, _, H, W = in_capsules.size()
        in_capsules = in_capsules.view(batch_size*self.in_caps,self.in_dim,4,H,W)
        predictions = self.preds(in_capsules)

        _,_,_, H, W = predictions.size()
        predictions = predictions.view(batch_size, self.in_caps, self.out_caps*self.out_dim, 4, H, W)
        predictions = predictions.view(batch_size, self.in_caps, self.out_caps, self.out_dim, 4, H, W)
        
        out_capsules = self.wl_routing(predictions,k,ITER)
        return out_capsules

    def squash(self, inputs, dim):
        norm = torch.norm(inputs, p=2, dim=dim, keepdim=True)
        scale = norm**2 / (1 + norm**2) / (norm + 1e-8)
        return scale * inputs

    def cosine_similarity(self,predictions,eps=1e-8):
        dot_product = torch.matmul(predictions,predictions.transpose(-1,-2))
        norm_sq = torch.norm(predictions,dim=-1,keepdim=True)**2 
        eps_matrix = eps*torch.ones_like(norm_sq)
        norm_sq = torch.max(norm_sq,eps_matrix)
        similarity_matrix = dot_product/norm_sq
        return similarity_matrix

    def wl_routing(self,predictions,k=5,ITER=3):
        batch_size,_,_,_,_, H, W = predictions.size()
        predictions_permute = predictions.permute(0,2,4,5,6,1,3)#(batch_size,num_out_capsules,4,H,W,num_in_capsules,out_capsule_dim)
        predictions_permute = self.layer_norm(predictions_permute)
        affinity_matrices = self.cosine_similarity(predictions_permute)#(batch_size,num_out_capsules,4,H,W,num_in_capsules,num_in_capsules)
        weight_scores = torch.sum(affinity_matrices,dim=-1,keepdim=True)#(batch_size,num_out_capsules,4,H,W,num_in_capsules,1)
        k_nearest_indices = torch.topk(affinity_matrices,k,dim=6)[1]#(batch_size,num_out_caps,4,H,W,num_in_caps,k)
        k_nearest_indices = k_nearest_indices.permute(0,1,2,3,4,6,5)#(batch_size,num_out_caps,4,H,W,k,num_in_caps)
        for it in range(ITER):
            weight_scores = torch.repeat_interleave(weight_scores,self.in_caps,-1)#batch_size,num_out_caps,4,H,W,num_in_caps,num_in_caps)   
            selected_weights = torch.gather(weight_scores,5,k_nearest_indices)#(batch_size,num_out_caps,4,H,W,k,num_in_caps)
            #print(selected_weights.size())
            #assert(False)
            weight_scores = torch.mean(selected_weights,dim=5,keepdim=True).permute(0,1,2,3,4,6,5)#(batch_size,num_out_caps,4,H,W,num_in_caps,1)
        weight_scores = F.softmax(weight_scores,dim=6)#(batch_size,num_out_caps,4,H,W,num_in_caps,1)
        weight_scores = (weight_scores).permute(0,5,1,6,2,3,4)#(batch_size,num_in_capsules,num_out_capsules,1,H,W)
        #weight_scores = (torch.ones(batch_size,self.in_caps,self.out_caps,1,H,W)*(1.0/float(self.in_caps))).to(DEVICE)
        s_j = (weight_scores * predictions).sum(dim=1)
        v_j = self.squash(s_j,dim=3)
        return v_j.squeeze(dim=1)

class CapsuleDimension(nn.Module):
      def __init__(self, in_dim, out_dim):
          super(CapsuleDimension, self).__init__()
          self.in_dim = in_dim
          self.out_dim = out_dim
          self.conv = P4ConvP4(in_dim, out_dim, 1)
      
      def forward(self, capsule):
          num_capsule = capsule.size(1)
          capsule = capsule.view(-1, self.in_dim, 4, 1, 1)
          capsule = self.conv(capsule)
          capsule = capsule.view(-1, num_capsule, 4, 1)
          return capsule
            

class ResidualSovnet(nn.Module):
    def __init__(self):
        super(ResidualSovnet, self).__init__()
        self.in_capsule_dim = 16
        self.num_capsules = 32
        self.conv1 = ResNetBlock(3, 128, 2)
        self.primary_capsules = PrimaryCapsules(128, self.num_capsules, self.in_capsule_dim, 16,16)
        self.layer1 = ConvolutionalCapsules(32,16,32,16,3,stride=2)
        self.layer2 = ConvolutionalCapsules(32,16,32,16,3)#self._make_layer(block, 16, 16, 16, 16, num_blocks[1], dilation=2)
        self.layer3 = ConvolutionalCapsules(32,16,32,16,3)#self._make_layer(block, 8, 8, 10, 16, num_blocks[2], stride=2, padding=1, dilation=2)
        #self.layer4 = self._make_layer(block, 10, 16, 16, 16, num_blocks[3], stride=2, padding=0, dilation=4)
        self.layer4 = ConvolutionalCapsules(32,16,32,16,3,padding=1)
        self.class_capsules = ConvolutionalCapsules(32,16,10,16,3)
        self.linear = CapsuleDimension(16,1)#nn.Linear(36,1)
        #self.reconstruction_layer = ReconstructionLayer(10,16,31,3).to(device)
        
    def _make_layer(self, block, num_in_capsule, in_capsule_dim, num_out_capsule, out_capsule_dim, num_blocks, dilation=1, padding=0):
        #strides = [stride] + [1]*(num_blocks-1)
        dilations = [dilation] + [1]*(num_blocks-1)
        layers = []
        for dilation in dilations:
            layers.append(block(num_in_capsule, in_capsule_dim, num_out_capsule, out_capsule_dim, dilation=dilation))
            num_in_capsule = num_out_capsule
            in_capsule_dim = out_capsule_dim
        return nn.Sequential(*layers)

    def forward(self, x):
        out = self.conv1(x)
        out = self.primary_capsules(out)
        out = self.layer1(out)
        out = self.layer2(out)
        out = self.layer3(out)
        out = self.layer4(out)
        class_capsules = self.class_capsules(out)
        class_capsules = class_capsules.squeeze(4).squeeze(4)
        class_capsules = class_capsules.permute(0,1,3,2).contiguous()
        class_capsules = self.linear(class_capsules).squeeze(3)
        class_capsules, _ = torch.max(class_capsules,dim=2)
        #class_capsules = class_capsules.squeeze(dim=4).squeeze(4)
        #class_capsules_norm = torch.norm(class_capsules,dim=2,keepdim=False)
        #max_length_indices_per_type = torch.max(class_capsules_norm,dim=2)[1]  
        #masked = class_capsules.new_tensor(torch.eye(class_capsules.size(3)))
        #masked = masked.index_select(dim=0, index=max_length_indices_per_type.data.view(-1))
        #masked = masked.view(class_capsules.size(0),class_capsules.size(1),-1).unsqueeze(2)
        #class_capsules = (class_capsules*masked).sum(3)
        #class_capsules = torch.norm(class_capsules,dim=2)
        #reconstructions, masked = self.reconstruction_layer(class_capsules,target)
        return class_capsules#, reconstructions, masked
  
def get_activations(capsules):
    return torch.norm(capsules, dim=2).squeeze()
       
def get_predictions(activations):
    max_length_indices = activations.max(dim=1)[1].squeeze()#(batch_size)
    predictions = activations.new_tensor(torch.eye(100))
    predictions = predictions.index_select(dim=0,index=max_length_indices)
    return predictions
