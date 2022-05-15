import torch
import numpy as np
from src.utils import fixed_length_partitions
from src.space import LieGroup
import sympy as sp
from functools import reduce
import operator
import math
import itertools as it

dtype = torch.double


class SO(LieGroup):
    '''
    SO(dim), special orthogonal group of degree dim
    '''

    def __init__(self, dim: int, order: int):
        '''
        :param dim: dimension of the space
        :param order: order of approximation. Number of eigenspaces under consideration.
        '''
        super().__init__()

        self.dim = dim
        self.rank = dim // 2
        self.order = order
        if self.dim % 2 == 0:
            self.rho = np.arange(self.rank)[::-1]
        else:
            self.rho = np.arange(self.rank)[::-1] + 0.5

        if dim <= 2 or dim == 4:
            raise ValueError("Dimensions 1, 2, 4 are not supported")

        self.signatures, self.lb_eigenspaces_dims, self.lb_eigenvalues, = self._generate_signatures(self.order)
        self.lb_eigenbases_sums = [SOCharacter(self.dim, signature, eigen_dim)
                                   for signature, eigen_dim in zip(self.signatures, self.lb_eigenspaces_dims)]

    def dist(self, x, y):
        return torch.arccos(torch.dot(x, y))

    def difference(self, x, y):
        return x @ y.T

    def rand(self, n=1):
        h = torch.randn((n, self.dim, self.dim), dtype=dtype)
        q, r = torch.linalg.qr(h)
        diag_sign = torch.diag_embed(torch.diagonal(torch.sign(r), dim1=-2, dim2=-1))
        q = torch.bmm(q, diag_sign)
        det_sign = torch.sign(torch.det(q))
        sign_matirx = torch.eye(self.dim, dtype=dtype).reshape((-1, self.dim, self.dim)).repeat((n, 1, 1))
        sign_matirx[:, 0, 0] = det_sign
        q = q @ sign_matirx
        return q

    def _generate_signatures(self, order):
        '''
        Representations of SO can be enumerated by partitions of size dim that we will call signatures.
        :param int order: number of eigenfunctions that will be returned
        :return signatures, eigenspaces_dims, eigenvalues: top order representations sorted by eigenvalues.
        '''
        signatures = []
        if self.dim == 3:
            signature_sum = order
        else:
            signature_sum = 20
        for signature_sum in range(0, signature_sum):
            for i in range(1, self.rank + 1):
                for signature in fixed_length_partitions(signature_sum, i):
                    signature.extend([0] * (self.rank-i))
                    signatures.append(tuple(signature))
                    if self.dim % 2 == 0 and signature[-1] != 0:
                        signature[-1] = -signature[-1]
                        signatures.append(tuple(signature))

        def _compute_dim(signature):
            if self.dim % 2 == 1:
                qs = [pk + self.rank - k - 1 / 2 for k, pk in enumerate(signature)]
                rep_dim = reduce(operator.mul, (2 * qs[k] / math.factorial(2 * k + 1) for k in range(0, self.rank))) \
                             * reduce(operator.mul, ((qs[i] - qs[j]) * (qs[i] + qs[j])
                                                  for i, j in it.combinations(range(self.rank), 2)), 1)
                return int(round(rep_dim))
            else:
                qs = [pk + self.rank - k - 1 if k != self.rank - 1 else abs(pk) for k, pk in enumerate(signature)]
                rep_dim = int(reduce(operator.mul, (2 / math.factorial(2 * k) for k in range(1, self.rank)))
                              * reduce(operator.mul, ((qs[i] - qs[j]) * (qs[i] + qs[j])
                                             for i, j in it.combinations(range(self.rank), 2)), 1))
                return int(round(rep_dim))

        def _compute_eigenvalue(sgn):
            np_sgn = np.array(sgn)
            return np.linalg.norm(self.rho + np_sgn) ** 2 - np.linalg.norm(self.rho) ** 2

        signatures_vals = []
        for sgn in signatures:
            dim = _compute_dim(sgn)
            eigenvalue = _compute_eigenvalue(sgn)
            signatures_vals.append([sgn, dim, eigenvalue])

        signatures_vals.sort(key=lambda x: x[2])
        signatures_vals = signatures_vals[:order]

        signatures = np.array([x[0] for x in signatures_vals])
        dims = torch.tensor([x[1] for x in signatures_vals], dtype=dtype)
        eigenvalues = torch.tensor([x[2] for x in signatures_vals])

        return signatures, dims, eigenvalues


class SOCharacter(torch.nn.Module):
    def __init__(self, dim, signature, eigen_dim):
        super().__init__()
        self.dim = dim
        self.signature = signature
        self.rank = dim // 2
        self.eigen_dim = eigen_dim

    def torus_embed(self, x):
        #TODO :check
        eigv = torch.linalg.eigvals(x)
        sorted_ind = torch.sort(torch.view_as_real(eigv), dim=1).indices[:, :, 0]
        eigv = torch.gather(eigv, dim=1, index=sorted_ind)
        gamma = eigv[:, 0:-1:2]
        return gamma

    def xi0(self, qs, gamma):
        a = torch.stack([torch.pow(gamma, q) + torch.pow(gamma, -q) for q in qs], dim=-1)
        return torch.det(a)

    def xi1(self, qs, gamma):
        a = torch.stack([torch.pow(gamma, q) - torch.pow(gamma, -q) for q in qs], dim=-1)
        return torch.det(a)

    def chi(self, x):
        eps = 0#1e-3*torch.tensor([1+1j]).cuda().item()
        gamma = self.torus_embed(x)
        if self.dim % 2:
            qs = [pk + self.rank - k - 1 / 2 for k, pk in enumerate(self.signature)]
            return self.xi1(qs, gamma) / \
                   self.xi1([k - 1 / 2 for k in range(self.rank, 0, -1)], gamma)
        else:
            qs = [pk + self.rank - k - 1 if k != self.rank - 1 else abs(pk)
                  for k, pk in enumerate(self.signature)]
            if self.signature[-1] == 0:
                return self.xi0(qs, gamma) / \
                       self.xi0(list(reversed(range(self.rank))), gamma)
            else:
                sign = math.copysign(1, self.signature[-1])
                return (self.xi0(qs, gamma) + self.xi1(qs, gamma) * sign) / \
                       self.xi0(list(reversed(range(self.rank))), gamma)

    def close_to_eye(self, x):
        d = x.shape[1]  # x = [n,d,d]
        x_ = x.reshape((x.shape[0], -1))  # [n, d * d]

        eye = torch.reshape(torch.torch.eye(d, dtype=dtype).reshape((-1, d * d)), (1, d * d))  # [1, d * d]
        eyes = eye.repeat(x.shape[0], 1)  # [n, d * d]

        return torch.all(torch.isclose(x_, eyes), dim=1)

    def forward(self, x, y):
        n, m = x.shape[0], y.shape[1] # number of x and y
        # [n,m,d,d] -> [n*m, d, d]
        x_flatten = torch.reshape(x, (-1, x.shape[2], x.shape[3]))
        y_flatten = torch.reshape(y, (-1, y.shape[2], y.shape[3]))

        x_yT = torch.bmm(x_flatten, torch.transpose(y_flatten, -1, -2))  # [n*m, d, d]
        chi_flatten = self.eigen_dim * self.chi(x_yT)  # [n*m]

        close_to_eye = self.close_to_eye(x_yT)  # [n*m]

        chi_flatten = torch.where(close_to_eye,
                                  self.eigen_dim * self.eigen_dim * torch.ones_like(chi_flatten), chi_flatten)

        return chi_flatten.reshape(n, m)
