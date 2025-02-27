import torch
from functools import reduce
import math

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


# This function was copied from torch.optim.LBFGS
def _cubic_interpolate(x1, f1, g1, x2, f2, g2, bounds=None):
    # ported from https://github.com/torch/optim/blob/master/polyinterp.lua
    # Compute bounds of interpolation area
    if bounds is not None:
        xmin_bound, xmax_bound = bounds
    else:
        xmin_bound, xmax_bound = (x1, x2) if x1 <= x2 else (x2, x1)

    # Code for most common case: cubic interpolation of 2 points
    #   w/ function and derivative values for both
    # Solution in this case (where x2 is the farthest point):
    #   d1 = g1 + g2 - 3*(f1-f2)/(x1-x2);
    #   d2 = sqrt(d1^2 - g1*g2);
    #   min_pos = x2 - (x2 - x1)*((g2 + d2 - d1)/(g2 - g1 + 2*d2));
    #   t_new = min(max(min_pos,xmin_bound),xmax_bound);
    d1 = g1 + g2 - 3 * (f1 - f2) / (x1 - x2)
    d2_square = d1 ** 2 - g1 * g2
    if d2_square >= 0:
        d2 = d2_square.sqrt()
        if x1 <= x2:
            min_pos = x2 - (x2 - x1) * ((g2 + d2 - d1) / (g2 - g1 + 2 * d2))
        else:
            min_pos = x1 - (x1 - x2) * ((g1 + d2 - d1) / (g1 - g2 + 2 * d2))
        return min(max(min_pos, xmin_bound), xmax_bound)
    else:
        return (xmin_bound + xmax_bound) / 2.


# This function was copied from torch.optim.LBFGS
# This function use the strong wolfe conditions to get optimal step length
def _strong_wolfe(obj_func,
                  x,
                  t,
                  d,
                  f,
                  g,
                  gtd,
                  c1=1e-4,
                  c2=0.9,
                  tolerance_change=1e-9,
                  max_ls=25):
    # ported from https://github.com/torch/optim/blob/master/lswolfe.lua
    d_norm = d.abs().max()
    g = g.clone(memory_format=torch.contiguous_format)
    # evaluate objective and gradient using initial step
    f_new, g_new = obj_func(x, t, d)
    ls_func_evals = 1
    gtd_new = g_new.dot(d)

    # bracket an interval containing a point satisfying the Wolfe criteria
    t_prev, f_prev, g_prev, gtd_prev = 0, f, g, gtd
    done = False
    ls_iter = 0
    while ls_iter < max_ls:
        # check conditions
        if f_new > (f + c1 * t * gtd) or (ls_iter > 1 and f_new >= f_prev):
            bracket = [t_prev, t]
            bracket_f = [f_prev, f_new]
            bracket_g = [g_prev, g_new.clone(memory_format=torch.contiguous_format)]
            bracket_gtd = [gtd_prev, gtd_new]
            break

        if abs(gtd_new) <= -c2 * gtd:
            bracket = [t]
            bracket_f = [f_new]
            bracket_g = [g_new]
            done = True
            break

        if gtd_new >= 0:
            bracket = [t_prev, t]
            bracket_f = [f_prev, f_new]
            bracket_g = [g_prev, g_new.clone(memory_format=torch.contiguous_format)]
            bracket_gtd = [gtd_prev, gtd_new]
            break

        # interpolate
        min_step = t + 0.01 * (t - t_prev)
        max_step = t * 10
        tmp = t
        t = _cubic_interpolate(
            t_prev,
            f_prev,
            gtd_prev,
            t,
            f_new,
            gtd_new,
            bounds=(min_step, max_step))

        # next step
        t_prev = tmp
        f_prev = f_new
        g_prev = g_new.clone(memory_format=torch.contiguous_format)
        gtd_prev = gtd_new
        f_new, g_new = obj_func(x, t, d)
        ls_func_evals += 1
        gtd_new = g_new.dot(d)
        ls_iter += 1

    # reached max number of iterations?
    if ls_iter == max_ls:
        bracket = [0, t]
        bracket_f = [f, f_new]
        bracket_g = [g, g_new]

    # zoom phase: we now have a point satisfying the criteria, or
    # a bracket around it. We refine the bracket until we find the
    # exact point satisfying the criteria
    insuf_progress = False
    # find high and low points in bracket
    low_pos, high_pos = (0, 1) if bracket_f[0] <= bracket_f[-1] else (1, 0)
    while not done and ls_iter < max_ls:
        # line-search bracket is so small
        if abs(bracket[1] - bracket[0]) * d_norm < tolerance_change:
            break

        # compute new trial value
        t = _cubic_interpolate(bracket[0], bracket_f[0], bracket_gtd[0],
                               bracket[1], bracket_f[1], bracket_gtd[1])

        # test that we are making sufficient progress:
        # in case `t` is so close to boundary, we mark that we are making
        # insufficient progress, and if
        #   + we have made insufficient progress in the last step, or
        #   + `t` is at one of the boundary,
        # we will move `t` to a position which is `0.1 * len(bracket)`
        # away from the nearest boundary point.
        eps = 0.1 * (max(bracket) - min(bracket))
        if min(max(bracket) - t, t - min(bracket)) < eps:
            # interpolation close to boundary
            if insuf_progress or t >= max(bracket) or t <= min(bracket):
                # evaluate at 0.1 away from boundary
                if abs(t - max(bracket)) < abs(t - min(bracket)):
                    t = max(bracket) - eps
                else:
                    t = min(bracket) + eps
                insuf_progress = False
            else:
                insuf_progress = True
        else:
            insuf_progress = False

        # Evaluate new point
        f_new, g_new = obj_func(x, t, d)
        ls_func_evals += 1
        gtd_new = g_new.dot(d)
        ls_iter += 1

        if f_new > (f + c1 * t * gtd) or f_new >= bracket_f[low_pos]:
            # Armijo condition not satisfied or not lower than lowest point
            bracket[high_pos] = t
            bracket_f[high_pos] = f_new
            bracket_g[high_pos] = g_new.clone(memory_format=torch.contiguous_format)
            bracket_gtd[high_pos] = gtd_new
            low_pos, high_pos = (0, 1) if bracket_f[0] <= bracket_f[1] else (1, 0)
        else:
            if abs(gtd_new) <= -c2 * gtd:
                # Wolfe conditions satisfied
                done = True
            elif gtd_new * (bracket[high_pos] - bracket[low_pos]) >= 0:
                # old high becomes new low
                bracket[high_pos] = bracket[low_pos]
                bracket_f[high_pos] = bracket_f[low_pos]
                bracket_g[high_pos] = bracket_g[low_pos]
                bracket_gtd[high_pos] = bracket_gtd[low_pos]

            # new point becomes new low
            bracket[low_pos] = t
            bracket_f[low_pos] = f_new
            bracket_g[low_pos] = g_new.clone(memory_format=torch.contiguous_format)
            bracket_gtd[low_pos] = gtd_new

    # return stuff
    t = bracket[low_pos]
    f_new = bracket_f[low_pos]
    g_new = bracket_g[low_pos]
    return f_new, g_new, t, ls_func_evals


class LSR1(torch.optim.Optimizer):
    """
    .. Class of the limited memory symmetric rank-1 update.
        The first six functions are from torch.optim.LBFGS. 
        The step function has some parts which is from torch.optim.LBFGS.

    .. warning::
        This optimizer doesn't support per-parameter options and parameter
        groups (there can be only one).

    .. note::
        This is a very memory intensive optimizer. If it doesn't fit in memory
        try reducing the memory size, or decrease batch_size.

    Args:
        lr (float): learning rate (default: 1)
        max_iter (int): maximal number of iterations per optimization step
            (default: 20)
        tolerance_grad (float): termination tolerance on first order optimality
            (default: 1e-5).
        tolerance_change (float): termination tolerance on function
            value/parameter changes (default: 1e-15).
        tr_radius (float): initial radius of trust region (default = 0.00075).
        memory_size (int): memory size of S and Y (default: 3).
        mu (float): \in [0,1) hyperparameter for momentum (default: 0.75)
        nu (float): \in [0,1) hyperparameter for momentum (default: 0.75)
        alpha_S (float): \in [0,1] hyperparameter for momentum (default: 0)
        newton_maxit (int): max newton iteration for trust OBS solver (default: 5)
        cg_iter (int): max iteration for Trust Steihaug cg solver (default: 1000)
        line_search_fn (str): either 'strong_wolfe' or None (default: 'strong_wolfe').
        trust_solver (str): either 'OBS' or 'Cauchy_Point_Calculation' 
            or 'Steihaug_cg' (default: OBS)
    """

    def __init__(self,
                 params,
                 lr=1,
                 max_iter=20,
                 tolerance_grad=1e-15,
                 tolerance_change=1e-15,
                 tr_radius=0.00075,
                 memory_size=3,
                 mu=0.75,
                 nu=0.75,
                 alpha_S=0,
                 newton_maxit=5,
                 cg_iter=1000,
                 line_search_fn="strong_wolfe",
                 trust_solver="OBS"):
        defaults = dict(
            lr=lr,
            max_iter=max_iter,
            tolerance_grad=tolerance_grad,
            tolerance_change=tolerance_change,
            tr_radius=tr_radius,
            memory_size=memory_size,
            mu=mu,
            nu=nu,
            alpha_S=alpha_S,
            newton_maxit=newton_maxit,
            cg_iter=cg_iter,
            line_search_fn=line_search_fn,
            trust_solver=trust_solver)
        super(LSR1, self).__init__(params, defaults)

        # From torch.optim.LBFGS 
        # it checks if is one dictionary with params
        if len(self.param_groups) != 1:
            raise ValueError("LSR1 doesn't support per-parameter options "
                             "(parameter groups)")

        # From torch.optim.LBFGS
        # unpack the paramaters
        self._params = self.param_groups[0]['params']
        self._numel_cache = None
        self.memory_size = self.param_groups[0]['memory_size']
        self.newton_maxit = self.param_groups[0]['newton_maxit']
        self.cg_iter = self.param_groups[0]['cg_iter']

    # From torch.optim.LBFGS
    def _numel(self):
        if self._numel_cache is None:
            self._numel_cache = reduce(lambda total, p: total + p.numel(), self._params, 0)
        return self._numel_cache

    # From torch.optim.LBFGS
    # flat the gradient
    def _gather_flat_grad(self):
        views = []
        for p in self._params:
            if p.grad is None:
                view = p.new(p.numel()).zero_()
            elif p.grad.is_sparse:
                view = p.grad.to_dense().view(-1)
            else:
                view = p.grad.view(-1)
            views.append(view)
        return torch.cat(views, 0)

    # From torch.optim.LBFGS
    # do the update step
    def _add_grad(self, step_size, update):
        offset = 0
        for p in self._params:
            numel = p.numel()
            # view as to avoid deprecated pointwise semantics
            p.add_(update[offset:offset + numel].view_as(p), alpha=step_size)
            offset += numel
        assert offset == self._numel()

    # From torch.optim.LBFGS
    def _clone_param(self):
        return [p.clone(memory_format=torch.contiguous_format) for p in self._params]

    # From torch.optim.LBFGS
    def _set_param(self, params_data):
        for p, pdata in zip(self._params, params_data):
            p.copy_(pdata)

    # From torch.optim.LBFGS
    def _directional_evaluate(self, closure, x, t, d):
        self._add_grad(t, d)
        loss = float(closure())
        flat_grad = self._gather_flat_grad()
        self._set_param(x)
        return loss, flat_grad

    def trust_solver_OBS(self, M, P, lamb_gamma, trust_radius, gamma, flat_grad, psi):
        """
      .. The function solve a trust region subproblem with the Orthonomal Basis SR1 method.
         This was copied from https://github.com/MATHinDL/sL_QN_TR.
         Look in Subroutines in TRsubproblem_solver_OBS.m.
         The matlab Code is translated to Python Code

      Args: 
          M (torch.tensor): a small matrix from the calculation of the Hessian matrix
          P (torch.tensor) : The orthogonal matrix P from the Hessian matrix
          lamb_gamma : eigenvalues of RM^{-1}R + gamma
          trust_radius (float) : trust_radius
          gamma (float) : the gamma for the initial Hessian matrix B_0= gamma * I_n
          flat_grad (torch.Tensor) : gradient vector
          psi (torch.tensor) : Y-B_0*S


      """

        def phi_phi_T(sigma, delta, a, lam):
            """

            Args:
                sigma: input
                delta: trust region radius
                a: P.T@g and the extension a_{m+1} = sqrt(||g||^2 - ||P.T@g||^2)
                lam: eigenvalues of  RM^{-1}R + gamma

            Returns:
                Value of phi and his derivative for the newton method

            """
            obs_tol = 1e-10
            t = lam + sigma
            if torch.sum(abs(a) < obs_tol) > 0 or torch.sum(abs(t) < obs_tol) > 0:
                llpll2 = 0
                llpll_prim = 0
                for i in range(len(a)):
                    if abs(a[i]) > obs_tol > abs(t[i]):
                        return -1 / trust_radius, 1 / obs_tol
                    elif abs(a[i]) > obs_tol and abs(t[i]) > obs_tol:
                        llpll2 = llpll2 + (a[i] / t[i]) ** 2
                        llpll_prim = llpll_prim + (a[i] ** 2 / t[i] ** 3)
                llpll = torch.sqrt(torch.abs(llpll2))
                return 1 / llpll - 1 / delta, llpll_prim / (llpll ** 3)
            llpll = torch.linalg.norm(a / t)
            return 1 / llpll - 1 / delta, torch.sum((a ** 2) / (t ** 3)) / (llpll ** 3)

        def phi(sigma, delta, a, lam):
            """

            Args:
                sigma: input
                delta: trust region radius
                a: P.T@g and the extension a_{m+1} = sqrt(||g||^2 - ||P.T@g||^2)
                lam: eigenvalues of RM^{-1}R + gamma

            Returns:
                Value of phi
            """
            obs_tol = 1e-10
            t = lam + sigma
            if torch.sum(abs(a) < obs_tol) > 0 or torch.sum(abs(t) < obs_tol) > 0:
                llpll2 = 0
                for i in range(len(a)):
                    if abs(a[i]) > obs_tol > abs(t[i]):
                        return -1 / trust_radius
                    elif abs(a[i]) > obs_tol and abs(t[i]) > obs_tol:
                        llpll2 = llpll2 + (a[i] / t[i]) ** 2
                return 1 / torch.sqrt(llpll2) - 1 / delta
            llpll = torch.linalg.norm(a / t)
            return 1 / llpll - 1 / delta

        def equation_p1(psi, M, sigma_star, flat_grad):
            """

            Args:
                psi: Y-B_0*S
                M: small matrix from the calculation of the Hessian matrix
                sigma_star: optimal sigam for the solution of the next step "sigma + gamma"
                flat_grad: Gradient

            Returns:
                return optimal step (Delta w^*), calculated with Sherman Morrison Woodbury formula
            """
            psi_T = torch.transpose(psi, 0, 1)
            Z = sigma_star * M + torch.matmul(psi_T, psi)
            f = torch.matmul(psi_T, flat_grad)
            Zf = torch.linalg.solve(Z, f)
            return -(flat_grad - torch.matmul(psi, Zf)) / sigma_star

        def equation_p2(sigma, gamma, g, a, lam, P, g_l):
            """

            Args:
                sigma: optimal sigma for phi
                gamma: B_0 = gamma* I_n
                g: Gradient
                a: P.T@g and the extension a_{m+1} = sqrt(||g||^2 - ||P.T@g||^2)
                lam: eigenvalues of  RM^{-1}R + gamma
                P: orthogonal Matrix P from the Hessian Matrix
                g_l: P.T@g

            Returns:
                return optimal step (Delta w^*), calculated with the Moore Penrose Pseudoinverse Matrix
            """
            t = lam + sigma
            idx = torch.abs(t) > 1e-10
            c = len(t)
            v = torch.zeros(c).to(device)
            v[idx] = a[idx] / (lam[idx] + sigma)
            if torch.abs(gamma + sigma) < 1e-10:
                p = -torch.matmul(P, v[:-1])
            else:
                p = -torch.matmul(P, v[:-1]) - (g - torch.matmul(P, g_l)) / (gamma + sigma)
            return p

        def equation_p3(lam_min, delta, p_hat, lam, P):
            """

            Args:
                lam_min: the smallest eigenvalue of M
                delta: trust region radius
                p_hat: step from the Moore Penrose Pseudoinverse
                lam: eigenvalues of  RM^{-1}R + gamma
                P: orthogonal Matrix P from the Hessian Matrix

            Returns:
                 return optimal step (Delta w^*), calculated with the Moore Penrose Pseudoinverse Matrix
                 and the unit vector from the eigenspace of lam_min
            """
            alpha = torch.sqrt(delta ** 2 - torch.matmul(p_hat, p_hat))
            if torch.abs(lam_min - lam[-2]) < 1e-10:
                u_min = P[:, -1] / torch.linalg.norm(P[:, -1])
                z_star = alpha * u_min
            else:
                n, k = P.shape[0], P.shape[1]
                e = torch.zeros(n)
                found = 0
                j = 0
                for i in range(k):
                    e[i] = 1
                    u_min = e - torch.matmul(P, torch.transpose(P, 0, 1)[:, i])
                    if torch.linalg.norm(u_min) > 1e-10:
                        found = 1
                        j = i
                        break
                    j = i
                    e[i] = 0
                if found == 0:
                    e[j + 1] = 1
                    u_min = e - torch.matmul(P, torch.transpose(P, 0, 1)[:, j + 1])
                u_min = u_min / torch.linalg.norm(u_min)
                z_star = alpha * u_min
            return p_hat + z_star

        def newton_method(sigma, delta, a, lam_all):
            """

            Args:
                sigma: optimal sigma for phi
                delta: trust region radius
                a: P.T@g and the extension a_{m+1} = sqrt(||g||^2 - ||P.T@g||^2)
                lam_all: [lamb + gamma, gamma]

            Returns:
                performs the newton method to get the optimal sigma for phi
                about 5 iterations
            """
            newton_tol = 1e-15
            k = 0
            s = sigma
            phi, phi_T = phi_phi_T(s, delta, a, lam_all)
            while torch.abs(phi) > newton_tol and k < self.newton_maxit:
                s = s - phi / phi_T
                phi, phi_T = phi_phi_T(s, delta, a, lam_all)
                k += 1
            return s

        lam_all = torch.zeros(lamb_gamma.shape[0] + 1).to(device)
        lam_all[-1] = gamma
        lam_all[:-1] = lamb_gamma
        lam_all = lam_all * (torch.abs(lam_all) > 1e-10)
        lam_min = torch.min(lam_all)
        g_ll = torch.matmul(torch.transpose(P, 0, 1), flat_grad)
        gg = torch.matmul(flat_grad, flat_grad)
        gl_gl = torch.matmul(g_ll, g_ll)
        llg_perbll = torch.sqrt(torch.abs(gg - gl_gl))
        if llg_perbll ** 2 < 1e-10:
            llg_perbll = 0
        a = torch.zeros(g_ll.shape[0] + 1).to(device)
        a[-1] = llg_perbll
        a[:-1] = g_ll
        if phi(0, trust_radius, a, lam_all) >= 0 and lam_min > 0:
            sigma_star = 0
            tau_star = gamma + sigma_star
            p_star = equation_p1(psi, M, tau_star, flat_grad)
            return p_star
        if lam_min <= 0 <= phi(-lam_min, trust_radius, a, lam_all):
            sigma_star = -lam_min
            p_star = equation_p2(sigma_star, gamma, flat_grad, a, lam_all, P, g_ll)
            if lam_min < 0:
                p_hat = p_star
                p_star = equation_p3(lam_min, trust_radius, p_hat, lam_all, P)
        else:
            if lam_min > 0:
                sigma_star = newton_method(0, trust_radius, a, lam_all)
            else:
                sigma_hat = max(torch.abs(a) / trust_radius - lam_all)
                if sigma_hat > -lam_min:
                    sigma_star = newton_method(sigma_hat, trust_radius, a, lam_all)
                else:
                    sigma_star = newton_method(-lam_min, trust_radius, a, lam_all)
            tau_star = sigma_star + gamma
            p_star = equation_p1(psi, M, tau_star, flat_grad)
        return p_star

    def calculate_M(self, S, Y, gamma):
        """

        Args:
            S: matrix which contains the old s as columns
            Y: matrix which contains the old y as columns
            gamma: B_0 = gamma * I_n

        Returns:
            M = L + L^T + S^T*B_0*S + diag(S^T*Y), Psi = Y-B_0*S
            L is a lower left triangular matrix of S^TY
        """

        # B_{k} = B_0 + Psi @ M^{-1} @ psi
        Psi = Y - gamma * S

        # calculate M = D+L+L^T-S*B_0*S
        SY = torch.mm(torch.transpose(S, 0, 1), Y)
        SS = gamma * torch.mm(torch.transpose(S, 0, 1), S)
        L = torch.tril(SY, diagonal=-1)
        M = L + torch.transpose(L, 0, 1) + SS
        mask_M = range(M.shape[0])
        M[mask_M, mask_M] = M[mask_M, mask_M] + torch.diag(SY)
        return M, Psi

    # calculate hess with limited memory method
    def calculate_hess(self, Psi, M_inverse):
        """

        Args:
            Psi: Y - gamma * S
            M_inverse: The Inverse of the Matrix M, see above in calculate_M

        Returns:
            Return a orthogonal Matrix P and the eignevalues of  RM^{-1}R
        """
        # thin q-r factorisation of phi
        Q, R = torch.linalg.qr(Psi, mode="reduced")

        # eigenvalues and eigenvectors of R@M^{-1}@R^T
        RMR = torch.mm(torch.mm(R, M_inverse), torch.transpose(R, 0, 1))
        RMR = (RMR + torch.transpose(RMR, 0, 1)) / 2
        lamb, U = torch.linalg.eig(RMR)

        # create last orthogonal matrix P = QU and return
        # there are no imaginary parts, but the function torch.linalg.eig return
        # complex values, so we have to extract these
        return torch.mm(Q, torch.real(U)), torch.real(lamb)

    def update_SY(self, s, y, old_s, old_y, cond_rest):
        """
      .. Update S and Y. Pop the first if history_size is reached.

      Args:
          s (torch.Tensor): currently s
          y (torch.Tensor): currently y
          old_s (list): a list with the last s as tensors
          old_y (list):  a list with the last y as tensors
          cond_rest (float): one part of the condition to update S or Y, check update is well-defined

      """
        ys = y.dot(s)  # y*s
        if ys + cond_rest > 1e-10:
            # updating memory
            if len(old_s) == self.memory_size:
                # shift history by one (limited-memory)
                old_s.pop(0)
                old_y.pop(0)

            # store new direction/step
            old_s.append(s)
            old_y.append(y)

    def update_radius(self, r, trust_radius, s, T, rho):
        """
      .. Update Radius like Algorithmus 5 in
        "A minibatch stochastic Quasi-Newton method adapted for nonconvex deep learning problems"
        from Joshua D. Griffin, Majid Jahani, Martin Takáč, Seyedalireza Yektamaram, Wenwen Zhou
        with a little change. The 0.1 in the first if is here 0.5.

      Args:
          r (float): ratio of actual and predicted reduction
          s (torch.tensor) : A vector w_k -w_{k-1}
          trust_radius (float): trust radius
          T (float) : A iteration parameter, it is used to do the radius stochastic
          rho (float) : A iteration parameter, it is used to do the radius stochastic
      """
        rho = 0.5 * T * rho - r
        T = 0.5 * T + 1
        rho = rho / T
        norm_s = torch.linalg.norm(s)
        if rho < 0.5:
            trust_radius = min(trust_radius, norm_s)
        if rho >= 0.5 and norm_s >= trust_radius:
            trust_radius = 2 * trust_radius
        return trust_radius, rho, T

    def trust_solver_cauchy(self, flat_grad, hess_1, hess_2, trust_radius):
        """
      .. Solves a trust region subproblem with the Cauchy Point Calculation.

      Args: 
          flat_grad (torch.tensor) : gradient vector
          hess_1 (torch.tensor) : P*diag(gamma+lambda)*P^T = hess, hess_1 = P*diag(gamma+lambda)
          hess_2 (torch.tensor) : P*diag(gamma+lambda)*P^T = hess, hess_2 = P^T
          trust_radius (float) : the trust radius
      """
        gH = torch.matmul(flat_grad, hess_1)
        Hg = torch.matmul(hess_2, flat_grad)
        cauchy_cond = torch.matmul(gH, Hg)
        if cauchy_cond <= 0:
            tau = 1
        else:
            tau = min(torch.linalg.norm(flat_grad) ** 3 / (cauchy_cond * trust_radius), 1)
        return -tau * trust_radius / torch.linalg.norm(flat_grad) * flat_grad

    def trust_solver_steihaug(self, flat_grad, hess_1, hess_2, trust_radius):
        """
      .. Solves a trust subproblem with the Steihaug CG Method.
        The solution for tau can be calculated with the quadratic formula.
        The hyperparameters are from https://d-nb.info/1219852988/34.

      Args: 
          flat_grad (torch.tensor) : gradient vector
          hess_1 (torch.tensor) : P*diag(gamma+lambda)*P^T = hess, hess_1 = P*diag(gamma+lambda)
          hess_2 (torch.tensor) : P*diag(gamma+lambda)*P^T = hess, hess_2 = P^T
          trust_radius (float) : trust region radius
      """
        z = torch.zeros(len(flat_grad)).to(device)
        r = flat_grad
        d = -r
        g_norm = torch.linalg.norm(flat_grad)
        delta = min(0.5, torch.sqrt(g_norm)) * g_norm
        fH = torch.matmul(flat_grad, hess_1)
        Hf = torch.matmul(hess_2, flat_grad)
        if torch.matmul(fH, Hf) <= 0:
            return -trust_radius / torch.linalg.norm(flat_grad) * flat_grad
        for _ in range(self.cg_iter):
            dH = torch.matmul(d, hess_1)
            Hd = torch.matmul(hess_2, d)
            dHd = torch.matmul(dH, Hd)
            dz = torch.matmul(d, z)
            dd = torch.matmul(d, d)
            zz = torch.matmul(z, z)
            if dHd <= 0:
                tau = (-2 * dz + torch.sqrt((-2 * dz) ** 2 - 4 * dd * (zz - trust_radius ** 2))) / (2 * dd)
                return z + tau * d
            rr = torch.matmul(r, r)
            alpha = rr / dHd
            z_ersatz = z
            z = z + alpha * d
            if torch.linalg.norm(z) >= trust_radius:
                tau = (-2 * dz + torch.sqrt((-2 * dz) ** 2 - 4 * dd * (zz - trust_radius ** 2))) / (2 * dd)
                return z_ersatz + tau * d
            r = r + alpha * torch.matmul(hess_1, torch.matmul(hess_2, d))
            if torch.linalg.norm(r) < delta:
                return z
            beta = torch.matmul(r, r) / rr
            d = -r + beta * dd
        return z

    @torch.no_grad()
    def step(self, closure):
        """Performs a single optimization step.

        Args:
            closure (callable): A closure that reevaluates the model
                and returns the loss.
        """
        assert len(self.param_groups) == 1

        # From torch.optim.LBFGS
        # Make sure the closure is always called with grad enabled
        closure = torch.enable_grad()(closure)

        # load hyperparameter and settings
        group = self.param_groups[0]
        lr = group['lr']
        max_iter = group['max_iter']
        tolerance_grad = group['tolerance_grad']
        tolerance_change = group['tolerance_change']
        line_search_fn = group['line_search_fn']
        trust_solver = group['trust_solver']
        tr_radius = group['tr_radius']
        mu = group['mu']
        nu = group['nu']
        alpha_S = group['alpha_S']

        # From torch.optim.LBFGS
        # NOTE: LSR1 has only global state, but we register it as state for
        # the first param, because this helps with casting in load_state_dict
        state = self.state[self._params[0]]
        # step 1
        state.setdefault('restart', 1)

        # get loss
        # evaluate initial f(x) and df/dx
        # From torch.optim.LBFGS
        orig_loss = closure()
        loss = float(orig_loss)
        flat_grad = self._gather_flat_grad()
        opt_cond = flat_grad.abs().max() <= tolerance_grad

        # From torch.optim.LBFGS
        # optimal condition
        if opt_cond:
            return orig_loss

        # tensors cached in state (for tracing)
        delta_w = state.get('d')
        v = state.get('v')
        alpha = state.get('alpha')
        old_s = state.get('old_s')
        old_y = state.get('old_y')
        prev_flat_grad = state.get('prev_flat_grad')
        trust_radius = state.get('trust_radius')
        s = state.get('s')
        y = state.get('y')
        T = state.get('T')
        rho = state.get('rho')

        # check if we need initial radius or can we get from last step
        if trust_radius is None:
            trust_radius = tr_radius

        # dimension of the data
        dim_hess = flat_grad.shape[0]
        n_iter = 0

        while n_iter < max_iter:  # step 2
            n_iter += 1
            ############################################################
            ####       compute gradient descent direction           ####
            ############################################################
            if state['restart'] == 1:  # step 3-9
                state['restart'] = 0
                # the first direction is the normal gradient
                # initialize parameters of the first step or for restart
                delta_w = flat_grad.neg()
                old_s = []
                old_y = []
                P = torch.ones(1).to(device)
                L = torch.ones(1).to(device)
                gamma = 1
                v = torch.zeros(dim_hess).to(device)
                s = torch.zeros(dim_hess).to(device)
                y = torch.zeros(dim_hess).to(device)
                T = 0
                rho = 0
            else:  # step 10 to 14
                # stack the list to a tensor 
                S = torch.transpose(torch.stack(old_s), 0, 1)
                Y = torch.transpose(torch.stack(old_y), 0, 1)

                # calculate gamma like in Stabilizied Barzilai-Borwein Method
                # from Oleg Burdakov, Yu-Hong Dai, Na Huang
                # step 11
                g_1 = torch.matmul(old_s[-1], old_y[-1]) / (torch.matmul(old_y[-1], old_y[-1]))
                g_2 = torch.matmul(old_s[-1], old_s[-1]) / (torch.matmul(old_s[-1], old_y[-1]))
                gamma = max(0.1, max(g_1, g_2))

                # calculate M and Psi
                # step 12
                M, psi = self.calculate_M(S, Y, gamma)

                # check singular
                l, _ = torch.linalg.eig(M)
                if min(abs(l)) < 1e-16:
                    state['restart'] = 1
                    break

                # calculate the inverse of M
                M_inverse = torch.linalg.solve(M, torch.eye(M.shape[0]).to(device))

                # calculate the components of the hessian matrix
                # step 13
                P, lamb = self.calculate_hess(psi, M_inverse)
                L = lamb * P
                P = torch.transpose(P, 0, 1)

                # get the new search direction with Trust Region
                # step 14
                if trust_solver == "Cauchy_Point_Calculation":
                    delta_w = self.trust_solver_cauchy(flat_grad, L, P, trust_radius)
                if trust_solver == "Steihaug_cg":
                    delta_w = self.trust_solver_steihaug(flat_grad, L, P, trust_radius)
                if trust_solver == "OBS":
                    delta_w = self.trust_solver_OBS(M, torch.transpose(P, 0, 1), lamb + gamma, trust_radius, gamma,
                                                    flat_grad,
                                                    psi)
            # do some other options: momentum etc.
            # step 16 - 19
            v = mu * v - nu * alpha_S * flat_grad + (1 - nu) * s
            v = min(1, trust_radius / torch.linalg.norm(v)) * v
            delta_w = (1 - nu) * delta_w + mu * v
            delta_w = min(1, trust_radius / torch.linalg.norm(delta_w)) * delta_w
            delta_w = delta_w.to(device)
            dg = abs(torch.matmul(delta_w, flat_grad))

            # check if the serch direction is too orthogonal to gradient
            # step 20 - 23
            d_norm = abs(torch.linalg.norm(delta_w))
            g_norm = abs(torch.linalg.norm(flat_grad))
            if min(dg, dg / d_norm) < g_norm * 5e-10:
                state['restart'] = 1
                break

            # We need this for the current prediction
            if len(L.shape) != 1:
                dH = torch.matmul(delta_w, L)
                Hd = torch.matmul(P, delta_w)
                dHd = torch.matmul(dH, Hd)
                dHd = dHd + gamma * torch.matmul(delta_w, delta_w)
            else:
                dHd = gamma * torch.matmul(delta_w, delta_w)

            #############################################################
            #######     set lr, s, previous loss and flat_grad  #########
            #############################################################
            # From torch.optim.LBFGS
            # directional derivative
            gtd = flat_grad.dot(delta_w)
            # check descent direction
            # step 24 - 26
            if gtd > 0:
                delta_w = -delta_w

            # set s/alpha and a part of update condition/alpha
            s = torch.clone(delta_w).to(device)
            if len(L.shape) != 1:
                sH = torch.matmul(s, L)
                Hs = torch.matmul(P, s)
                cond_rest = torch.matmul(sH, Hs) + gamma * torch.matmul(s, s)
            else:
                cond_rest = gamma * torch.matmul(s, s)

            # From torch.optim.LBFGS
            # reset initial guess for step size
            # step 27
            if state['restart'] == 1:
                alpha = min(1., 1. / flat_grad.abs().sum()) * lr
            else:
                alpha = lr

            # update prev_flat_grad
            # From torch.optim.LBFGS
            if prev_flat_grad is None:
                prev_flat_grad = flat_grad.clone(memory_format=torch.contiguous_format)
            else:
                prev_flat_grad.copy_(flat_grad)

            prev_loss = loss

            #############################################################
            #######               gradient step                 #########
            #############################################################
            # From torch.optim.LBFGS
            # optional line search: user function   #step 31-32
            if line_search_fn is not None:
                # perform line search, using user function
                if line_search_fn != "strong_wolfe":
                    raise RuntimeError("only 'strong_wolfe' is supported")
                else:
                    x_init = self._clone_param()

                    def obj_func(x, t, d):
                        return self._directional_evaluate(closure, x, t, d)

                    # step 27
                    loss_t, flat_grad_t, alpha_t, _ = _strong_wolfe(
                        obj_func, x_init, alpha, delta_w, loss, flat_grad, gtd)

                # sometimes the search direction is so bad, that alpha can be zero
                # or very big. This produces nan in the loss
                # Avoid this and break
                if 1e-12 > alpha_t or alpha_t > 1000000:
                    state['restart'] = 1
                    break
                check_grad = torch.linalg.norm(flat_grad_t)
                if check_grad < 1e-12 or check_grad > 1000000:
                    state['restart'] = 1
                    break
                if loss_t > 1000000 or math.isnan(loss_t):
                    state['restart'] = 1
                    break
                loss = loss_t
                flat_grad = flat_grad_t
                opt_cond = flat_grad.abs().max() <= tolerance_grad
                alpha = alpha_t

                # step 28
                self._add_grad(alpha, delta_w)
                # step 29 - 32
                if opt_cond:
                    state['restart'] = 1
                    break
            else:
                # no line search, simply move with fixed-step
                # step 28
                self._add_grad(alpha, delta_w)
                if n_iter != max_iter:
                    with torch.enable_grad():
                        loss = float(closure())
                    flat_grad = self._gather_flat_grad()
                    opt_cond = flat_grad.abs().max() <= tolerance_grad
                    # step 29 - 32
                    if opt_cond:
                        state['restart'] = 1
                        break

            # step 33
            s = alpha * s
            # step 34
            y = flat_grad.sub(prev_flat_grad)
            cond_rest = alpha * cond_rest

            #############################################################
            #######               update S,Y                    #########
            #############################################################
            # step 35
            self.update_SY(s, y, old_s, old_y, cond_rest)
            if len(old_s) == 0:
                state['restart'] = 1
                break

            #############################################################
            #######               calculate ratio               #########
            #############################################################
            # step 36
            ared = prev_loss - loss
            # From torch.optim.LBFGS
            # step 37 - 40
            if abs(ared) < tolerance_change:
                state['restart'] = 1
                break
            # step 41
            pred = loss + torch.matmul(flat_grad, delta_w) + 0.5 * dHd
            # step 42
            r = ared / pred

            #############################################################
            #######               update radius                 #########
            #############################################################
            # step 43
            trust_radius, rho, T = self.update_radius(r, trust_radius, s, T, rho)

            # From torch.optim.LBFGS
            if n_iter == max_iter:
                break

        state['d'] = delta_w
        state['alpha'] = alpha
        state['old_s'] = old_s
        state['old_y'] = old_y
        state['prev_flat_grad'] = flat_grad
        state['trust_radius'] = trust_radius
        state['v'] = v
        state['s'] = s
        state['y'] = y
        state['T'] = T
        state['rho'] = rho

        return orig_loss
