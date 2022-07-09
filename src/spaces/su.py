import torch
#from functorch import vmap
from torch.autograd.functional import _vmap as vmap
import numpy as np
from src.space import CompactLieGroup, LBEigenspaceWithSum, LieGroupCharacter
from functools import reduce
import operator
import math
import itertools
from src.utils import vander_det, vander_det2, poly_eval_tensor
from scipy.special import chebyu
import sympy
import json
from pathlib import Path
dtype = torch.cdouble
device = 'cuda' if torch.cuda.is_available() else 'cpu'


class SU(CompactLieGroup):
    """SU(dim), special unitary group of degree dim."""

    def __init__(self, n: int, order: int):
        """
        :param dim: dimension of the space
        :param order: the order of approximation, the number of representations calculated
        """
        self.n = n
        self.dim = n*n-1
        self.rank = n-1
        self.order = order
        self.Eigenspace = SULBEigenspace

        self.rho = np.arange(self.n - 1, -self.n, -2) * 0.5

        super().__init__(order=order)

    def dist(self, x, y):
        raise NotImplementedError

    def difference(self, x, y):
        return x @ y.mH

    def rand(self, n=1):
        h = torch.randn((n, self.n, self.n), dtype=dtype, device=device)
        q, r = torch.linalg.qr(h)
        r_diag = torch.diagonal(r, dim1=-2, dim2=-1)
        r_diag_inv_phase = torch.conj(r_diag / torch.abs(r_diag))
        q *= r_diag_inv_phase[:, None]
        q_det = torch.det(q)
        q_det_inv_phase = torch.conj(q_det / torch.abs(q_det))
        q[:, :, 0] *= q_det_inv_phase[:, None]
        return q

    def generate_signatures(self, order):
        """Generate the signatures of irreducible representations

        Representations of SO(dim) can be enumerated by partitions of size dim, called signatures.
        :param int order: number of eigenfunctions that will be returned
        :return signatures: signatures of representations likely having the smallest LB eigenvalues
        """
        sign_vals_lim = order if self.n == 1 else 10 if self.n == 2 else 5
        signatures = list(itertools.combinations_with_replacement(range(sign_vals_lim, -1, -1), r=self.rank))
        signatures = [sgn + (0,) for sgn in signatures]
        signatures.sort()
        return signatures

    @staticmethod
    def inv(x: torch.Tensor):
        # (n, dim, dim)
        return torch.conj(torch.transpose(x, -2, -1))

    @staticmethod
    def close_to_id(x):
        d = x.shape[-1]  # x = [...,d,d]
        x_ = x.reshape(x.shape[:-2] + (-1,))  # [..., d * d]
        eyes = torch.broadcast_to(torch.flatten(torch.eye(d, dtype=dtype, device=device)), x_.shape)  # [..., d * d]
        return torch.all(torch.isclose(x_, eyes, atol=1e-5), dim=-1)

    @staticmethod
    def torus_embed(x):
        return torch.linalg.eigvals(x)


class SULBEigenspace(LBEigenspaceWithSum):
    """The Laplace-Beltrami eigenspace for the special unitary group."""

    def __init__(self, signature, *, manifold: SU):
        """
        :param signature: the signature of a representation
        :param manifold: the "parent" manifold, an instance of SO
        """
        super().__init__(signature, manifold=manifold)

    def compute_dimension(self):
        signature = self.index
        su = self.manifold
        rep_dim = reduce(operator.mul, (reduce(operator.mul, (signature[i - 1] - signature[j - 1] + j - i for j in
                                                              range(i + 1, su.n + 1))) / math.factorial(su.n - i)
                                        for i in range(1, su.n)))
        return int(round(rep_dim))

    def compute_lb_eigenvalue(self):
        sgn = np.array(self.index, dtype=np.float)
        # transform the signature into the same basis as rho
        sgn -= np.mean(sgn)
        rho = self.manifold.rho
        lb_eigenvalue = (np.linalg.norm(rho + sgn) ** 2 - np.linalg.norm(rho) ** 2)  # / (2 * self.manifold.n)
        return lb_eigenvalue.item()

    def compute_basis_sum(self):
        return SUCharacterDenominatorFree(representation=self)

class SUCharacter(LieGroupCharacter):
    """Representation character for special unitary group"""

    def chi(self, gammas):
        n = self.representation.manifold.n
        signature = self.representation.index
        # eps = 0#1e-3*torch.tensor([1+1j]).cuda().item()
        qs = [pk + n - k - 1 for k, pk in enumerate(signature)]
        numer_mat = torch.stack([torch.pow(gammas, q) for q in qs], dim=-1)
        vander = vander_det2(gammas)
        return torch.det(numer_mat) / vander


class SUCharacterDenominatorFree(LieGroupCharacter):
    def __init__(self, *, representation: SULBEigenspace, precomputed=True):
        super().__init__(representation=representation)
        if precomputed:
            group_name = '{}({})'.format(self.representation.manifold.__class__.__name__,
                                         self.representation.manifold.n)
            file_path = Path(__file__).with_name('precomputed_characters.json')
            with file_path.open('r') as file:
                character_formulas = json.load(file)
                try:
                    cs, ms = character_formulas[group_name][str(self.representation.index)]
                    self.coeffs, self.monoms = (torch.tensor(data, dtype=torch.int, device=device) for data in (cs, ms))
                except KeyError as e:
                    raise KeyError('Unable to retrieve character parameters for signature {} of {}, '
                                   'perhaps it is not precomputed.'.format(e.args[0], group_name)) from None

    def _compute_character_formula(self):
        n = self.representation.manifold.n
        gammas = sympy.symbols(' '.join('g{}'.format(i) for i in range(1, n + 1)))
        qs = [pk + n - k - 1 for k, pk in enumerate(self.representation.index)]
        numer_mat = sympy.Matrix(n, n, lambda i, j: gammas[i]**qs[j])
        vander = sympy.prod(gammas[i] - gammas[j] for i, j in itertools.combinations(range(n), r=2))
        p = sympy.Poly(sympy.exquo(sympy.det(numer_mat), vander), gammas)
        coeffs = list(map(int, p.coeffs()))
        monoms = [list(map(int, monom)) for monom in p.monoms()]
        return coeffs, monoms

    def chi(self, gammas):
        char_val = torch.zeros(gammas.shape[:-1], dtype=dtype, device=device)
        for coeff, monom in zip(self.coeffs, self.monoms):
            char_val += coeff * torch.prod(gammas ** monom, dim=-1)
        return char_val


class SU2Character(LieGroupCharacter):
    def __init__(self, *, representation: LBEigenspaceWithSum):
        super().__init__(representation=representation)
        self.coeffs = chebyu(self.representation.index[0]).coef

    def chi(self, x):
        trace = torch.einsum('...ii->...', x)
        return poly_eval_tensor(trace / 2, self.coeffs)
