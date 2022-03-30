import math

from typing import Optional, Tuple
from torch_geometric.typing import Adj, OptTensor, PairTensor
import torch
from torch import nn, einsum, broadcast_tensors
import torch
from torch import Tensor
from torch.nn import Parameter
from torch_geometric.nn.conv.gcn_conv import gcn_norm
from torch_scatter import scatter_add
from torch_sparse import SparseTensor, matmul, fill_diag, sum as sparsesum, mul
from torch_geometric.nn.conv import MessagePassing
from torch_geometric.utils import add_remaining_self_loops
from torch_geometric.utils.num_nodes import maybe_num_nodes
from torch_geometric.typing import Adj, Size, OptTensor, Tensor

def glorot(tensor):
    if tensor is not None:
        stdv = math.sqrt(6.0 / (tensor.size(-2) + tensor.size(-1)))
        tensor.data.uniform_(-stdv, stdv)

        
def zeros(tensor):
    if tensor is not None:
        tensor.data.fill_(0)
class CoorsNorm(nn.Module):
    def __init__(self, eps = 1e-8):
        super().__init__()
        self.eps = eps
        self.fn = nn.Sequential(nn.LayerNorm(1), nn.GELU())

    def forward(self, coors):
        norm = coors.norm(dim = -1, keepdim = True)
        normed_coors = coors / norm.clamp(min = self.eps)
        phase = self.fn(norm)
        return (phase * normed_coors)

    
class PEG_conv(MessagePassing):
    
    r"""The PEG layer from the `"Equivariant and Stable Positional Encoding for More Powerful Graph Neural Networks" <https://arxiv.org/abs/2203.00199>`_ paper
    
    
    Args:
        in_feats_dim (int): Size of input node features.
        pos_dim (int): Size of positional encoding.
        out_feats_dim (int): Size of output node features.
        edge_mlp_dim (int): We use MLP to make one to one mapping between the relative information and edge weight. 
                            edge_mlp_dim represents the hidden units dimension in the MLP. (default: 32)
        improved (bool, optional): If set to :obj:`True`, the layer computes
            :math:`\mathbf{\hat{A}}` as :math:`\mathbf{A} + 2\mathbf{I}`.
            (default: :obj:`False`)
        cached (bool, optional): If set to :obj:`True`, the layer will cache
            the computation of :math:`\mathbf{\hat{D}}^{-1/2} \mathbf{\hat{A}}
            \mathbf{\hat{D}}^{-1/2}` on first execution, and will use the
            cached version for further executions.
            This parameter should only be set to :obj:`True` in transductive
            learning scenarios. (default: :obj:`False`)
        add_self_loops (bool, optional): If set to :obj:`False`, will not add
            self-loops to the input graph. (default: :obj:`True`)
        normalize (bool, optional): Whether to add self-loops and compute
            symmetric normalization coefficients on the fly.
            (default: :obj:`True`)
        bias (bool, optional): If set to :obj:`False`, the layer will not learn
            an additive bias. (default: :obj:`True`)
        update_coors (bool): Whether to update positional encodings.
        use_formerinfo (bool): Whether to use previous layer's output to update node features.
        norm_coors (bool): Whether to normalize positional encodings. Only used when update_coors = True. 
        **kwargs (optional): Additional arguments of
            :class:`torch_geometric.nn.conv.MessagePassing`.
    """

    def __init__(self, in_feats_dim: int, pos_dim: int, out_feats_dim: int, edge_mlp_dim: int = 32,
                 improved: bool = False, cached: bool = False,
                 add_self_loops: bool = True, normalize: bool = True,
                 bias: bool = True, update_coors: bool = False,
                 use_formerinfo: bool = False, norm_coors = True, **kwargs):

        kwargs.setdefault('aggr', 'add')
        super(PEG_conv, self).__init__(**kwargs)

        self.in_feats_dim = in_feats_dim
        self.out_feats_dim = out_feats_dim
        self.pos_dim = pos_dim
        self.update_coors = update_coors
        self.use_formerinfo = use_formerinfo
        self.improved = improved
        self.cached = cached
        self.add_self_loops = add_self_loops
        self.normalize = normalize
        self.edge_mlp_dim = edge_mlp_dim
        self.coors_norm = CoorsNorm() if norm_coors else nn.Identity()

        self._cached_edge_index = None
        self._cached_adj_t = None
        
        self.edge_mlp1 = nn.Linear(1, edge_mlp_dim)
        self.edge_mlp2 = nn.Linear(edge_mlp_dim, 1)
        self.weight_withformer = Parameter(torch.Tensor(in_feats_dim + in_feats_dim, out_feats_dim))
        self.weight_noformer = Parameter(torch.Tensor(in_feats_dim, out_feats_dim))
        self.coors_mlp = nn.Sequential(
            nn.Linear(pos_dim, pos_dim * 4),
            nn.Linear(pos_dim * 4, 1)
        ) if update_coors else None

        if bias:
            self.bias = Parameter(torch.Tensor(out_feats_dim))
        else:
            self.register_parameter('bias', None)

        self.reset_parameters()

    def reset_parameters(self):
        glorot(self.weight_withformer)
        glorot(self.weight_noformer)
        zeros(self.bias)
        self._cached_edge_index = None
        self._cached_adj_t = None


    def forward(self,x: Tensor, edge_index: Adj, 
                edge_weight: OptTensor = None) -> Tensor:
        """"""
        
        coors, feats = x[:, :self.pos_dim], x[:, self.pos_dim:]
        
        if self.normalize:
            if isinstance(edge_index, Tensor):
                cache = self._cached_edge_index
                if cache is None:
                    edge_index, edge_weight = gcn_norm(  # yapf: disable
                        edge_index, edge_weight, feats.size(self.node_dim),
                        self.improved, self.add_self_loops)
                    if self.cached:
                        self._cached_edge_index = (edge_index, edge_weight)
                else:
                    edge_index, edge_weight = cache[0], cache[1]

            elif isinstance(edge_index, SparseTensor):
                cache = self._cached_adj_t
                if cache is None:
                    edge_index = gcn_norm(  # yapf: disable
                        edge_index, edge_weight, feats.size(self.node_dim),
                        self.improved, self.add_self_loops)
                    if self.cached:
                        self._cached_adj_t = edge_index
                else:
                    edge_index = cache
        else:
            print('We normalize the adjacent matrix in PEG.')
        
        
        rel_coors = coors[edge_index[0]] - coors[edge_index[1]]
        neighbour_coors = coors[edge_index[1]]
        rel_dist  = (rel_coors ** 2).sum(dim=-1, keepdim=True)

        # propagate_type: (x: Tensor, edge_weight: OptTensor)
        # pos: l2 norms
        # rel_coors: used in updating positional encodings, not required for PEG
        hidden_out, coors_out = self.propagate(edge_index, x = feats, edge_weight=edge_weight, pos=rel_dist, coors=coors, rel_coors=rel_coors, neighbour_coors = neighbour_coors,
                             size=None)
        
        

        if self.bias is not None:
            hidden_out += self.bias

        return torch.cat([coors_out, hidden_out], dim=-1)


    def message(self, x_i: Tensor, x_j: Tensor, edge_weight: OptTensor, pos) -> Tensor:
        PE_edge_weight = self.edge_mlp1(pos)
        PE_edge_weight = self.edge_mlp2(PE_edge_weight)
        PE_edge_weight = torch.sigmoid(PE_edge_weight)
        return x_j if edge_weight is None else PE_edge_weight * edge_weight.view(-1, 1) * x_j
    
    def propagate(self, edge_index: Adj, size: Size = None, **kwargs):
        """The initial call to start propagating messages.
            Args:
            `edge_index` holds the indices of a general (sparse)
                assignment matrix of shape :obj:`[N, M]`.
            size (tuple, optional) if none, the size will be inferred
                and assumed to be quadratic.
            **kwargs: Any additional data which is needed to construct and
                aggregate messages, and to update node embeddings.
        """
        size = self.__check_input__(edge_index, size)
        coll_dict = self.__collect__(self.__user_args__,
                                     edge_index, size, kwargs)
        msg_kwargs = self.inspector.distribute('message', coll_dict)
        aggr_kwargs = self.inspector.distribute('aggregate', coll_dict)
        update_kwargs = self.inspector.distribute('update', coll_dict)

        # get messages
        m_ij = self.message(**msg_kwargs)


        m_i = self.aggregate(m_ij, **aggr_kwargs)
        
        # update coors if specified
        if self.update_coors:
            coor_wij = self.coors_mlp(m_ij)
            kwargs["neighbour_coors"] = self.coors_norm(kwargs["neighbour_coors"])
            mhat_i = self.aggregate(coor_wij * kwargs["neighbour_coors"], **aggr_kwargs)
            coors_out = kwargs["coors"] + mhat_i
        else:
            coors_out = kwargs["coors"]
        
        
        hidden_feats = kwargs["x"]
        if self.use_formerinfo:
            hidden_out = torch.cat([hidden_feats, m_i], dim = -1)
            hidden_out = hidden_out @ self.weight_withformer
        else:
            hidden_out = m_i
            hidden_out = hidden_out @ self.weight_noformer
        



        # return tuple
        return self.update((hidden_out, coors_out), **update_kwargs)

    def message_and_aggregate(self, adj_t: SparseTensor, x: Tensor) -> Tensor:
        return matmul(adj_t, x, reduce=self.aggr)

    def __repr__(self):
        return '{}({}, {})'.format(self.__class__.__name__, self.in_channels,
                                   self.out_channels)