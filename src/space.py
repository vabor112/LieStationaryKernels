import torch
from abc import ABC, abstractmethod
import heapq
from src.utils import lazy_property
from src.utils import cartesian_prod

j = torch.tensor([1j]).item()  # imaginary unit
pi = 2*torch.acos(torch.zeros(1)).item()


class AbstractManifold(torch.nn.Module, ABC):
    """Abstract base class for Compact Lie Group, Compact Homogeneous space or Symmetric Space"""
    def __init__(self):

        super().__init__()

        # self.dim = None
        # self.order = None

    @abstractmethod
    def dist(self, x, y):
        # compute distance between x and y
        raise NotImplementedError

    def inv(self, y):
        """ For y in M computes -y"""
        raise NotImplementedError

    def pairwise_diff(self, x, y):
        """for x of size n and y of size n computes x_i-y_j and represent as array [n*m,...]"""
        y_inv = self.inv(y)
        x_, y_inv_ = cartesian_prod(x, y_inv) # [n,m,...] and [n,m,...]

        x_flatten = torch.reshape(x_, (-1, *(x_.size()[2:])))
        y_inv_flatten = torch.reshape(y_inv_, (-1, *(y_inv_.size()[2:])))

        x_yinv = torch.bmm(x_flatten, y_inv_flatten)  # [n*m, ...]
        return x_yinv

    # @abstractmethod
    # def difference(self, x, y):
    #     # Using group structure computes xy^{-1}
    #     pass

    @abstractmethod
    def rand(self, n=1):
        # returns random element with respect to haar measure
        raise NotImplementedError


class LieGroup(AbstractManifold, ABC):
    """Lie group abstract base class"""
    def __init__(self, *, order: int):
        """
        Generate the list of signatures of representations and pick those with smallest LB eigenvalues.
        :param order: the order of approximation, the number of representations calculated
        """
        super().__init__()
        lb_eigenspaces = [self.Eigenspace(signature, manifold=self) for signature in self.generate_signatures(order)]
        self.lb_eigenspaces = heapq.nsmallest(order, lb_eigenspaces, key=lambda eig: eig.lb_eigenvalue)

    @abstractmethod
    def generate_signatures(self, order) -> list:
        """Generate signatures of representations to enumerate them."""
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def inv(x):
        """Calculate the group inverse of a batch of group elements"""
        raise NotImplementedError


class HomogeneousSpace(AbstractManifold, ABC):
    pass


class NonCompactSymmetricSpace(AbstractManifold, ABC):
    """Symmetric space of form G/H abstract class"""
    def __init__(self):
        super().__init__()
        #self.lb_eigenspaces = None  # will be generated with respect to spectral measure

    def dist(self, x, y):
        raise NotImplementedError

    def generate_lb_eigenspaces(self, measure):
        """Generates Eigenspaces with respect to the measure"""
        raise NotImplementedError

    def rand_factor(self, n=1):
        """ Generate elements from H with respect to Haar measure on H """
        raise NotImplementedError

    def inv(self, x):
        """ For element x in G calculate x^{-1}"""
        raise NotImplementedError


class LBEigenfunction(ABC):
    """Laplace-Beltrami eigenfunction abstract base class"""

    def __init__(self, index, *, manifold: AbstractManifold):
        """
        :param index: the index of an LB eigenspace
        :param manifold: the "parent" manifold
        """
        self.index = index
        self.manifold = manifold
        self.lb_eigenvalue = self.compute_lb_eigenvalue()

    def compute_lb_eigenvalue(self):
        """Compute the Laplace-Beltrami eigenvalues of the eigenfunction."""
        raise NotImplementedError


class LBEigenspace(ABC):
    """Laplace-Beltrami eigenspace abstract base class"""
    def __init__(self, index, *, manifold: AbstractManifold):
        """
        :param index: the index of an LB eigenspace
        :param manifold: the "parent" manifold
        """
        self.index = index
        self.manifold = manifold
        self.dimension = self.compute_dimension()
        self.lb_eigenvalue = self.compute_lb_eigenvalue()

    @abstractmethod
    def compute_dimension(self):
        """Compute the dimension of the Laplace-Beltrami eigenspace."""
        raise NotImplementedError

    @abstractmethod
    def compute_lb_eigenvalue(self):
        """Compute the Laplace-Beltrami eigenvalues of the eigenspace."""
        raise NotImplementedError


class LBEigenspaceWithBasis(LBEigenspace, ABC):
    """Laplace-Beltrami eigenspace ABC in case orthonormal basis is available"""
    @lazy_property
    def basis(self):
        basis = self.compute_basis()
        return basis

    @abstractmethod
    def compute_basis(self):
        """Compute an orthonormal basis of the eigenspace."""
        raise NotImplementedError


class LBEigenspaceWithSum(LBEigenspace, ABC):
    """Laplace-Beltrami eigenspace ABC in case the sum function of an orthonormal basis paired products is available"""
    @lazy_property
    def basis_sum(self):
        basis_sum = self.compute_basis_sum()
        return basis_sum

    @abstractmethod
    def compute_basis_sum(self):
        """Compute the sum of the orthonormal basis paired products."""
        raise NotImplementedError


class NonCompactSymmetricSpaceExp(torch.nn.Module, ABC):
    """For x in G computes e^{(i*lmd+rho)a(xh^{-1})}"""
    def __init__(self, lmd, shift, manifold):
        super().__init__()
        self.lmd = lmd  # shape is (m, r)
        self.shift = shift  # shape is (m,...)
        self.manifold = manifold

        self.order = lmd.size()[0]
        self.dim = self.manifold.dim
        self.rho = self.compute_rho()  # shape is (r,)

    def iwasawa_decomposition(self, x):
        """For x in G computes Iwasawa decomposition x = n(x)a(x)h(x)"""
        raise NotImplementedError

    def compute_rho(self):
        raise NotImplementedError

    def forward(self, x):
        # shape x is (n, ...)
        n = x.shape[0]
        x_shift_flatten = self.manifold.pairwise_diff(x, self.shift) # (n * m, ...)
        _, a_flatten, _ = self.iwasawa_decomposition(x_shift_flatten)  # shape (n * m, rank)
        log_a_flatten = torch.log(a_flatten).type(torch.cdouble)
        a = log_a_flatten.view(n, self.order, -1)

        lin_func = j*self.lmd + self.rho[None, :] # (m, rank)
        inner_prod = torch.einsum('nmr,mr->nm', a, lin_func)
        return torch.exp(inner_prod)  # shape (n, m)


class LieGroupCharacter(torch.nn.Module, ABC):
    """Lie group representation character abstract base class"""
    def __init__(self, *, representation: LBEigenspace):
        super().__init__()
        self.representation = representation

    def chi(self, x, y):
        raise NotImplementedError

    def forward(self, x, y):
        # x - n*d*d, y - m*d*d
        n, m = x.shape[0], y.shape[1] # number of x and y
        # [n,m,d,d] -> [n*m, d, d]
        x_flatten = torch.reshape(x, (-1, x.shape[2], x.shape[3]))
        y_flatten = torch.reshape(y, (-1, y.shape[2], y.shape[3]))

        y_inv = self.representation.manifold.inv(y_flatten)
        x_yinv = torch.bmm(x_flatten, y_inv)  # [n*m, d, d]
        chi_flatten = self.representation.dimension * self.chi(x_yinv)  # [n*m]

        close_to_eye = self.close_to_eye(x_yinv)  # [n*m]

        chi_flatten = torch.where(close_to_eye,
                                  self.representation.dimension**2 * torch.ones_like(chi_flatten), chi_flatten)

        return chi_flatten.reshape(n, m)