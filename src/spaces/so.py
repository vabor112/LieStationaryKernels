
import torch
import numpy as np
from src.utils import fixed_length_partitions
from src.space import CompactLieGroup, LBEigenspaceWithSum, LieGroupCharacter
from functools import reduce
import operator
import math
import itertools
from more_itertools import always_iterable
import sympy
#from functorch import vmap
from geomstats.geometry.special_orthogonal import _SpecialOrthogonalMatrices
from torch.autograd.functional import _vmap as vmap

dtype = torch.float64
device = 'cuda' if torch.cuda.is_available() else 'cpu'


class SO(CompactLieGroup):
    """SO(dim), special orthogonal group of degree dim."""

    def __init__(self, n: int, order: int):
        """
        :param dim: dimension of the space
        :param order: the order of approximation, the number of representations calculated
        """
        if n <= 2:
            raise ValueError("Dimensions 1, 2 are not supported")
        self.n = n
        self.dim = n * (n-1)//2
        self.rank = n // 2
        self.order = order
        self.Eigenspace = SOLBEigenspace
        if self.n % 2 == 0:
            self.rho = np.arange(self.rank-1, -1, -1)
        else:
            self.rho = np.arange(self.rank-1, -1, -1) + 0.5
        super().__init__(order=order)
        for irrep in self.lb_eigenspaces:
            irrep.basis_sum._compute_character_formula()

    def difference(self, x, y):
        return x @ y.T

    def dist(self, x, y):
        raise NotImplementedError

    def rand(self, n=1):
        h = torch.randn((n, self.n, self.n), device=device, dtype=dtype)
        q, r = torch.linalg.qr(h)
        r_diag_sign = torch.sign(torch.diagonal(r, dim1=-2, dim2=-1))
        q *= r_diag_sign[:, None]
        q_det_sign = torch.sign(torch.det(q))
        q[:, :, 0] *= q_det_sign[:, None]
        return q

    def generate_signatures(self, order):
        """Generate the signatures of irreducible representations

        Representations of SO(dim) can be enumerated by partitions of size dim, called signatures.
        :param int order: number of eigenfunctions that will be returned
        :return signatures: signatures of representations likely having the smallest LB eigenvalues
        """
        signatures = []
        if self.n == 3:
            signature_sum = order
        else:
            signature_sum = 20
        for signature_sum in range(0, signature_sum):
            for i in range(0, self.rank + 1):
                for signature in fixed_length_partitions(signature_sum, i):
                    signature.extend([0] * (self.rank-i))
                    signatures.append(tuple(signature))
                    if self.n % 2 == 0 and signature[-1] != 0:
                        signature[-1] = -signature[-1]
                        signatures.append(tuple(signature))
        return signatures

    @staticmethod
    def inv(x: torch.Tensor):
        # (n, dim, dim)
        return torch.transpose(x, -2, -1)

    @staticmethod
    def close_to_id(x):
        d = x.shape[-1]  # x = [...,d,d]
        x_ = x.reshape(x.shape[:-2] + (-1,))  # [..., d * d]
        eyes = torch.broadcast_to(torch.flatten(torch.eye(d, dtype=dtype, device=device)), x_.shape)  # [..., d * d]
        return torch.all(torch.isclose(x_, eyes, atol=1e-5), dim=-1)


class SOLBEigenspace(LBEigenspaceWithSum):
    """The Laplace-Beltrami eigenspace for the special orthogonal group."""
    def __init__(self, signature, *, manifold: SO):
        """
        :param signature: the signature of a representation
        :param manifold: the "parent" manifold, an instance of SO
        """
        super().__init__(signature, manifold=manifold)

    def compute_dimension(self):
        signature = self.index
        so = self.manifold
        if so.n % 2 == 1:
            qs = [pk + so.rank - k - 1 / 2 for k, pk in enumerate(signature)]
            rep_dim = reduce(operator.mul, (2 * qs[k] / math.factorial(2 * k + 1) for k in range(0, so.rank))) \
                      * reduce(operator.mul, ((qs[i] - qs[j]) * (qs[i] + qs[j])
                                              for i, j in itertools.combinations(range(so.rank), 2)), 1)
            return int(round(rep_dim))
        else:
            qs = [pk + so.rank - k - 1 if k != so.rank - 1 else abs(pk) for k, pk in enumerate(signature)]
            rep_dim = int(reduce(operator.mul, (2 / math.factorial(2 * k) for k in range(1, so.rank)))
                          * reduce(operator.mul, ((qs[i] - qs[j]) * (qs[i] + qs[j])
                                                  for i, j in itertools.combinations(range(so.rank), 2)), 1))
            return int(round(rep_dim))

    def compute_lb_eigenvalue(self):
        np_sgn = np.array(self.index)
        rho = self.manifold.rho
        return np.linalg.norm(rho + np_sgn) ** 2 - np.linalg.norm(rho) ** 2

    def compute_basis_sum(self):
        return SOCharacterDenominatorFree(representation=self)
        # if self.manifold.n == 3:
        #     return SO3Character(representation=self)
        # else:
        #     return SOCharacter(representation=self)


class SOCharacter(LieGroupCharacter):
    """Representation character for special orthogonal group"""
    # @staticmethod
    def torus_embed(self, x):
        if self.representation.manifold.n % 2 == 1:
            eigvals = torch.linalg.eigvals(x)
            sorted_ind = torch.sort(torch.view_as_real(eigvals), dim=-2).indices[..., 0]
            eigvals = torch.gather(eigvals, dim=-1, index=sorted_ind)
            gamma = eigvals[..., 0:-1:2]
            return gamma
        else:
            eigvals, eigvecs = torch.linalg.eig(x)
            # c is a matrix transforming x into its canonical form (with 2x2 blocks)
            c = torch.zeros_like(eigvecs)
            c[..., ::2] = eigvecs[..., ::2].real
            c[..., 1::2] = eigvecs[..., ::2].imag
            c *= math.sqrt(2)
            eigvals[..., 0] **= torch.det(c)
            gamma = eigvals[..., ::2]
            return gamma

    @staticmethod
    def xi0(qs, gamma):
        a = torch.stack([torch.pow(gamma, q) + torch.pow(gamma, -q) for q in qs], dim=-1)
        return torch.det(a)

    @staticmethod
    def xi1(qs, gamma):
        a = torch.stack([torch.pow(gamma, q) - torch.pow(gamma, -q) for q in qs], dim=-1)
        return torch.det(a)

    def chi(self, x):
        rank = self.representation.manifold.rank
        signature = self.representation.index
        # eps = 0#1e-3*torch.tensor([1+1j]).cuda().item()
        gamma = self.torus_embed(x)
        if self.representation.manifold.n % 2:
            qs = [pk + rank - k - 1 / 2 for k, pk in enumerate(signature)]
            return self.xi1(qs, gamma) / \
                   self.xi1([k - 1 / 2 for k in range(rank, 0, -1)], gamma)
        else:
            qs = [pk + rank - k - 1 if k != rank - 1 else abs(pk)
                  for k, pk in enumerate(signature)]
            if signature[-1] == 0:
                return self.xi0(qs, gamma) / \
                       self.xi0(list(reversed(range(rank))), gamma)
            else:
                sign = math.copysign(1, signature[-1])
                return (self.xi0(qs, gamma) + self.xi1(qs, gamma) * sign) / \
                       (1 * self.xi0(list(reversed(range(rank))), gamma))


class SOCharacterDenominatorFree(LieGroupCharacter):
    def __init__(self, *, representation: SOLBEigenspace):
        super().__init__(representation=representation)
        self._character_formula_computed = False

    def _compute_character_formula(self):
        # print('computing character formula for {}'.format(self.representation.index))
        n = self.representation.manifold.n
        rank = self.representation.manifold.rank
        signature = self.representation.index
        gammas = sympy.symbols(' '.join('g{}'.format(i + 1) for i in range(rank)))
        gammas = tuple(always_iterable(gammas))
        gammas_conj = sympy.symbols(' '.join('gc{}'.format(i + 1) for i in range(rank)))
        gammas_conj = tuple(always_iterable(gammas_conj))
        if n % 2:
            gammas_sqrt = sympy.symbols(' '.join('gr{}'.format(i + 1) for i in range(rank)))
            gammas_sqrt = tuple(always_iterable(gammas_sqrt))
            gammas_conj_sqrt = sympy.symbols(' '.join('gcr{}'.format(i + 1) for i in range(rank)))
            gammas_conj_sqrt = tuple(always_iterable(gammas_conj_sqrt))
            def xi1(qs):
                mat = sympy.Matrix(rank, rank, lambda i, j: gammas_sqrt[i]**qs[j]-gammas_conj_sqrt[i]**qs[j])
                return sympy.det(mat)
            # qs = [sympy.Integer(2*pk + 2*rank - 2*k - 1) / 2 for k, pk in enumerate(signature)]
            qs = [2 * pk + 2 * rank - 2 * k - 1 for k, pk in enumerate(signature)]
            # denom_pows = [sympy.Integer(2*k - 1) / 2 for k in range(rank, 0, -1)]
            denom_pows = [2 * k - 1 for k in range(rank, 0, -1)]
            numer = xi1(qs)
            denom = xi1(denom_pows)
            expr = sympy.ratsimpmodprime(numer / denom, [gr * gcr - 1 for gr, gcr in zip(gammas_sqrt, gammas_conj_sqrt)])
            expr = expr.subs([gr ** 2, g] for gr, g in zip(gammas_sqrt, gammas))
            expr = expr.subs([grc ** 2, gc] for grc, gc in zip(gammas_conj_sqrt, gammas_conj))
        else:
            def xi0(qs):
                mat = sympy.Matrix(rank, rank, lambda i, j: gammas[i] ** qs[j] + gammas_conj[i] ** qs[j])
                return sympy.det(mat)
            def xi1(qs):
                mat = sympy.Matrix(rank, rank, lambda i, j: gammas[i] ** qs[j] - gammas_conj[i] ** qs[j])
                return sympy.det(mat)
            qs = [pk + rank - k - 1 if k != rank - 1 else abs(pk) for k, pk in enumerate(signature)]
            pm = signature[-1]
            numer = xi0(qs)
            if pm:
                numer += (1 if pm > 0 else -1) * xi1(qs)
            denom = xi0(list(reversed(range(rank))))
            expr = sympy.ratsimpmodprime(numer/denom, [g*gc-1 for g, gc in zip(gammas, gammas_conj)])
        p = sympy.Poly(expr, gammas + gammas_conj)
        self.coeffs = torch.tensor(list(map(int, p.coeffs())), dtype=torch.int, device=device)
        self.monoms = torch.tensor([list(map(int, monom)) for monom in p.monoms()], dtype=torch.int, device=device)
        self._character_formula_computed = True

    def torus_embed(self, x):
        if self.representation.manifold.n % 2 == 1:
            eigvals = torch.linalg.eigvals(x)
            sorted_ind = torch.sort(torch.view_as_real(eigvals), dim=-2).indices[..., 0]
            eigvals = torch.gather(eigvals, dim=-1, index=sorted_ind)
            gamma = eigvals[..., 0:-1:2]
            return gamma
        else:
            eigvals, eigvecs = torch.linalg.eig(x)
            # c is a matrix transforming x into its canonical form (with 2x2 blocks)
            c = torch.zeros_like(eigvecs)
            c[..., ::2] = eigvecs[..., ::2].real
            c[..., 1::2] = eigvecs[..., ::2].imag
            c *= math.sqrt(2)
            eigvals[..., 0] **= torch.det(c)
            gamma = eigvals[..., ::2]
            return gamma

    def chi(self, x):
        if not self._character_formula_computed:
            self._compute_character_formula()
        gammas = self.torus_embed(x)
        gammas = torch.cat((gammas, gammas.conj()), dim=-1)
        char_val = torch.zeros(gammas.shape[:-1], dtype=torch.cdouble, device=device)
        for coeff, monom in zip(self.coeffs, self.monoms):
            char_val += coeff * torch.prod(gammas ** monom, dim=-1)
        return char_val


class SO3Character(LieGroupCharacter):
    @staticmethod
    def torus_embed(x):
        cos = (vmap(torch.trace)(x) - 1) / 2
        cos = torch.clip(cos, -1.0, 1.0)
        gamma = cos + 1j * torch.sqrt(1-torch.square(cos))
        return gamma

    def chi(self, x):
        l = self.representation.index[0]
        gamma = self.torus_embed(x)
        numer = torch.pow(gamma, l+0.5) - torch.pow(torch.conj(gamma), l+0.5)
        denom = torch.sqrt(gamma) - torch.sqrt(torch.conj(gamma))
        return numer / denom
