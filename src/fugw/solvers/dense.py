from functools import partial

import time

import torch

from fugw.solvers.utils import (
    BaseSolver,
    compute_approx_kl,
    compute_kl,
    compute_quad_kl,
    solver_ibpp,
    solver_mm,
    solver_sinkhorn,
)
from fugw.utils import console


class FUGWSolver(BaseSolver):
    """Solver computing dense solutions"""

    def local_biconvex_cost(
        self, pi, transpose, data_const, tuple_weights, hyperparams
    ):
        """
        Before each block coordinate descent (BCD) step,
        the local cost matrix is updated.
        This local cost is a matrix of size (n, m)
        which evaluates the cost between every pair of points
        of the source and target distributions.
        Then, we run a BCD (sinkhorn, ibpp or mm) step
        which makes use of this cost to update the transport plans.
        """

        rho_s, rho_t, eps, alpha, reg_mode = hyperparams
        ws, wt, ws_dot_wt = tuple_weights
        X_sqr, Y_sqr, X, Y, D = data_const
        if transpose:
            X_sqr, Y_sqr, X, Y = X_sqr.T, Y_sqr.T, X.T, Y.T

        pi1, pi2 = pi.sum(1), pi.sum(0)

        cost = torch.zeros_like(D)

        # Avoid unnecessary calculation of UGW when alpha = 0
        if alpha != 1 and D is not None:
            wasserstein_cost = D
            cost += (1 - alpha) / 2 * wasserstein_cost

        # or UOT when alpha = 1
        if alpha != 0:
            A = X_sqr @ pi1
            B = Y_sqr @ pi2
            gromov_wasserstein_cost = (
                A[:, None] + B[None, :] - 2 * X @ pi @ Y.T
            )

            cost += alpha * gromov_wasserstein_cost

        # or when cost is balanced
        if rho_s != float("inf") and rho_s != 0:
            marginal_cost_dim1 = compute_approx_kl(pi1, ws)
            cost += rho_s * marginal_cost_dim1
        if rho_t != float("inf") and rho_t != 0:
            marginal_cost_dim2 = compute_approx_kl(pi2, wt)
            cost += rho_t * marginal_cost_dim2

        if reg_mode == "joint":
            entropic_cost = compute_approx_kl(pi, ws_dot_wt)
            cost += eps * entropic_cost

        return cost

    def fugw_loss(self, pi, gamma, data_const, tuple_weights, hyperparams):
        """
        Returns scalar fugw loss, which is a combination of:
        - a Wasserstein loss on features
        - a Gromow-Wasserstein loss on geometries
        - marginal constraints on the computed OT plan
        - an entropic regularisation
        """

        rho_s, rho_t, eps, alpha, reg_mode = hyperparams
        ws, wt, ws_dot_wt = tuple_weights
        X_sqr, Y_sqr, X, Y, D = data_const

        pi1, pi2 = pi.sum(1), pi.sum(0)
        gamma1, gamma2 = gamma.sum(1), gamma.sum(0)

        loss = 0

        if alpha != 1 and D is not None:
            wasserstein_loss = (D * pi).sum() + (D * gamma).sum()
            loss += (1 - alpha) / 2 * wasserstein_loss

        if alpha != 0:
            A = (X_sqr @ gamma1).dot(pi1)
            B = (Y_sqr @ gamma2).dot(pi2)
            C = (X @ gamma @ Y.T) * pi
            gromov_wasserstein_loss = A + B - 2 * C.sum()
            loss += alpha * gromov_wasserstein_loss

        if rho_s != float("inf") and rho_s != 0:
            marginal_constraint_dim1 = compute_quad_kl(pi1, gamma1, ws, ws)
            loss += rho_s * marginal_constraint_dim1
        if rho_t != float("inf") and rho_t != 0:
            marginal_constraint_dim2 = compute_quad_kl(pi2, gamma2, wt, wt)
            loss += rho_t * marginal_constraint_dim2

        if reg_mode == "joint":
            entropic_regularization = compute_quad_kl(
                pi, gamma, ws_dot_wt, ws_dot_wt
            )
        elif reg_mode == "independent":
            entropic_regularization = compute_kl(pi, ws_dot_wt) + compute_kl(
                gamma, ws_dot_wt
            )

        entropic_loss = loss + eps * entropic_regularization

        return loss.item(), entropic_loss.item()

    def solve(
        self,
        alpha=0.5,
        rho_s=1,
        rho_t=1,
        eps=1e-2,
        reg_mode="joint",
        F=None,
        Ds=None,
        Dt=None,
        ws=None,
        wt=None,
        init_plan=None,
        init_duals=None,
        solver="sinkhorn",
        verbose=False,
    ):
        """
        Function running the BCD iterations.

        Parameters
        ----------
        alpha: float
        rho_s: float
        rho_t: float
        eps: float
        reg_mode: str
        F: matrix of size n x m.
            Kernel matrix between the source and target features.
        Ds: matrix of size n x n
        Dt: matrix of size m x m
        ws: ndarray(n), None
            Measures assigned to source points.
        wt: ndarray(m), None
            Measures assigned to target points.
        init_plan: matrix of size n x m if not None.
            Initialisation matrix for coupling.
        init_duals: tuple or None
            Initialisation duals for coupling.
        solver: "sinkhorn", "mm", "ibpp"
            Solver to use.
        verbose: bool, optional, defaults to False
            Log solving process.

        Returns
        -------
        res: dict
            Dictionary containing the following keys:
                pi: torch.Tensor of size n x m
                    Sample matrix.
                gamma: torch.Tensor of size d1 x d2
                    Feature matrix.
                duals_pi: tuple of torch.Tensor of size
                    Duals of pi
                duals_gamma: tuple of torch.Tensor of size
                    Duals of gamma
                loss_steps: list
                    BCD steps at the end of which the FUGW loss was evaluated
                loss: list
                    Values of FUGW loss
                loss_entropic: list
                    Values of FUGW loss with entropy
        """

        if rho_s == float("inf") and rho_t == float("inf") and eps == 0:
            raise ValueError(
                "This package does not handle balanced cases "
                "(ie infinite values of rho). "
                "You should have a look at POT (https://pythonot.github.io) "
                "and in particular at ot.gromov.fused_gromov_wasserstein "
                "(https://pythonot.github.io/gen_modules/ot.gromov.html"
                "#ot.gromov.fused_gromov_wasserstein)"
            )
        elif eps == 0 and (
            (rho_s == 0 and rho_t == float("inf"))
            or (rho_s == 0 and rho_t == float("inf"))
        ):
            raise ValueError(
                "Invalid rho and eps. Unregularized semi-relaxed GW is not "
                "supported."
            )

        # sanity check
        if solver == "mm" and (rho_s == float("inf") or rho_t == float("inf")):
            solver = "ibpp"
        if solver == "sinkhorn" and eps == 0:
            solver = "ibpp"

        device, dtype = Ds.device, Ds.dtype

        # constant data variables
        Ds_sqr = Ds**2
        Dt_sqr = Dt**2

        if alpha == 1 or F is None:
            alpha = 1
            F = None

        # measures on rows and columns
        if ws is None:
            n = Ds.shape[0]
            ws = torch.ones(n).to(device).to(dtype) / n
        if wt is None:
            m = Dt.shape[0]
            wt = torch.ones(m).to(device).to(dtype) / m
        ws_dot_wt = ws[:, None] * wt[None, :]

        # initialise coupling and dual vectors
        pi = ws_dot_wt if init_plan is None else init_plan
        gamma = pi

        if solver == "sinkhorn":
            if init_duals is None:
                duals_pi = (
                    torch.zeros_like(ws),
                    torch.zeros_like(wt),
                )
            else:
                duals_pi = init_duals
            duals_gamma = duals_pi
        elif solver == "mm":
            duals_pi, duals_gamma = None, None
        elif solver == "ibpp":
            if init_duals is None:
                duals_pi = (
                    torch.ones_like(ws),
                    torch.ones_like(wt),
                )
            else:
                duals_pi = init_duals
            duals_gamma = duals_pi

        compute_local_biconvex_cost = partial(
            self.local_biconvex_cost,
            data_const=(Ds_sqr, Dt_sqr, Ds, Dt, F),
            tuple_weights=(ws, wt, ws_dot_wt),
            hyperparams=(rho_s, rho_t, eps, alpha, reg_mode),
        )

        compute_fugw_loss = partial(
            self.fugw_loss,
            data_const=(Ds_sqr, Dt_sqr, Ds, Dt, F),
            tuple_weights=(ws, wt, ws_dot_wt),
            hyperparams=(rho_s, rho_t, eps, alpha, reg_mode),
        )

        self_solver_sinkhorn = partial(
            solver_sinkhorn,
            tuple_weights=(ws, wt, ws_dot_wt),
            train_params=(self.nits_uot, self.tol_uot, self.eval_uot),
        )

        self_solver_mm = partial(
            solver_mm,
            tuple_weights=(ws, wt),
            train_params=(self.nits_uot, self.tol_uot, self.eval_uot),
        )

        self_solver_ibpp = partial(
            solver_ibpp,
            tuple_weights=(ws, wt, ws_dot_wt),
            train_params=(
                self.nits_uot,
                self.ibpp_nits_sinkhorn,
                self.ibpp_eps_base,
                self.tol_uot,
                self.eval_uot,
            ),
            verbose=verbose,
        )

        # Initialize loss
        current_loss, current_loss_entropic = compute_fugw_loss(pi, gamma)
        loss_steps = [0]
        loss = [current_loss]
        loss_entropic = [current_loss_entropic]
        loss_times = [0]
        idx = 0
        err = None

        t0 = time.time()
        while (err is None or err > self.tol_bcd) and (idx < self.nits_bcd):
            pi_prev = pi.detach().clone()

            # Update gamma
            mp = pi.sum()
            new_rho_s = rho_s * mp
            new_rho_t = rho_t * mp
            new_eps = mp * eps if reg_mode == "joint" else eps
            uot_params = (new_rho_s, new_rho_t, new_eps)

            cost_gamma = compute_local_biconvex_cost(pi, transpose=True)
            if solver == "sinkhorn":
                duals_gamma, gamma = self_solver_sinkhorn(
                    cost_gamma, duals_gamma, uot_params
                )
            elif solver == "mm":
                gamma = self_solver_mm(cost_gamma, gamma, uot_params)
            if solver == "ibpp":
                duals_gamma, gamma = self_solver_ibpp(
                    cost_gamma, gamma, duals_gamma, uot_params
                )
            gamma = (mp / gamma.sum()).sqrt() * gamma

            # Update pi
            mg = gamma.sum()
            new_rho_s = rho_s * mg
            new_rho_t = rho_t * mg
            new_eps = mp * eps if reg_mode == "joint" else eps
            uot_params = (new_rho_s, new_rho_t, new_eps)

            cost_pi = compute_local_biconvex_cost(gamma, transpose=False)
            if solver == "sinkhorn":
                duals_pi, pi = self_solver_sinkhorn(
                    cost_pi, duals_pi, uot_params
                )
            elif solver == "mm":
                pi = self_solver_mm(cost_pi, pi, uot_params)
            elif solver == "ibpp":
                duals_pi, pi = self_solver_ibpp(
                    cost_pi, pi, duals_pi, uot_params
                )
            pi = (mg / pi.sum()).sqrt() * pi

            # Update error
            err = (pi - pi_prev).abs().sum().item()
            if idx % self.eval_bcd == 0:
                current_loss, current_loss_entropic = compute_fugw_loss(
                    pi, gamma
                )

                loss_steps.append(idx + 1)
                loss.append(current_loss)
                loss_entropic.append(current_loss_entropic)
                loss_times.append(time.time() - t0)

                if verbose:
                    console.log(
                        f"BCD step {idx+1}/{self.nits_bcd}\t"
                        f"FUGW loss:\t{current_loss} (base)\t"
                        f"{current_loss_entropic} (entropic)"
                    )

                if (
                    len(loss_entropic) >= 2
                    and abs(loss_entropic[-2] - loss_entropic[-1])
                    < self.early_stopping_threshold
                ):
                    break

            idx += 1

        if pi.isnan().any() or gamma.isnan().any():
            console.log("There is NaN in coupling")

        return {
            "pi": pi,
            "gamma": gamma,
            "duals_pi": duals_pi,
            "duals_gamma": duals_gamma,
            "loss_steps": loss_steps,
            "loss": loss,
            "loss_entropic": loss_entropic,
            "loss_times": loss_times,
        }
