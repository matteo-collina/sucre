# Copyright (C) 2022 Ifremer
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# Author: Clementin Boittiaux <boittiauxclementin at gmail dot com>

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import torch
import yaml
from PIL import Image
from scipy.optimize import minimize
from torch import Tensor

import gaussian_seathru
import loader
import normalization
import sfm


def initialize_sucre_parameters(
        image: sfm.Image,
        data: list[tuple[Tensor, Tensor, Tensor, Tensor]],
        matches_file: loader.MatchesFile,
        channel: int,
        mode: str = 'fast',
        device: str = 'cpu'
) -> tuple[Tensor, float, float, float]:
    print(f'Initialize parameters with Gaussian Sea-thru ({mode} mode).')
    match mode:
        case 'fast':
            Ic = loader.load_image(image.image_path)[:, :, channel].to(device)
            z = image.distance_map(loader.load_depth(image.depth_path).to(device))
            args_valid = z > 0
            Bc, betac, gammac = gaussian_seathru.solve_gaussian_seathru(Ic[args_valid], z[args_valid], linear_beta=True)
            Jc = torch.full((image.camera.height, image.camera.width), torch.nan, dtype=torch.float32, device=device)
            Jc[args_valid] = gaussian_seathru.compute_J(Ic[args_valid], z[args_valid], Bc, betac, gammac)
        case 'dense':
            Bc, betac, gammac = gaussian_seathru.solve_gaussian_seathru(
                *matches_file.load_Ic_z(channel, device=device), linear_beta=True
            )
            Bc, Jc = compute_Bc_Jc(image=image, data=data, betac=betac, gammac=gammac, device=device)
            Bc = Bc.item()
        case _:
            raise ValueError("only 'fast' and 'dense' are supported for `mode`.")
    return Jc.cpu(), Bc, betac, gammac


def iter_data(data: list[tuple[Tensor, Tensor, Tensor, Tensor]],
              device: str = 'cpu') -> tuple[Tensor, Tensor, Tensor, Tensor]:
    for u, v, z, Ic in data:
        yield u.to(device).long(), v.to(device).long(), z.to(device), Ic.to(device)


def compute_Bc_Jc(
        image: sfm.Image,
        data: list[tuple[Tensor, Tensor, Tensor, Tensor]],
        betac: Tensor | float,
        gammac: Tensor | float,
        device: str = 'cpu'
) -> tuple[Tensor, Tensor]:
    Xc_numerator = torch.zeros(image.camera.height, image.camera.width, dtype=torch.float32, device=device)
    Yc_numerator = torch.zeros(image.camera.height, image.camera.width, dtype=torch.float32, device=device)
    XYc_denominator = torch.zeros(image.camera.height, image.camera.width, dtype=torch.float32, device=device)
    Bc_numerator = torch.zeros(image.camera.height, image.camera.width, dtype=torch.float32, device=device)
    Bc_denominator = torch.zeros(image.camera.height, image.camera.width, dtype=torch.float32, device=device)

    for ui, vi, zi, Iic in iter_data(data, device=device):
        aic = torch.exp(-betac * zi)
        bic = 1 - torch.exp(-gammac * zi)
        Xc_numerator[vi, ui] += Iic * aic
        Yc_numerator[vi, ui] += bic * aic
        XYc_denominator[vi, ui] += torch.square(aic)

    Xc = Xc_numerator / XYc_denominator
    Yc = Yc_numerator / XYc_denominator

    for ui, vi, zi, Iic in iter_data(data, device=device):
        aic = torch.exp(-betac * zi)
        bic = 1 - torch.exp(-gammac * zi)
        Mic = Iic - Xc[vi, ui] * aic
        Nic = bic - Yc[vi, ui] * aic
        Bc_numerator[vi, ui] += Mic * Nic
        Bc_denominator[vi, ui] += torch.square(Nic)

    Bc = Bc_numerator.sum() / Bc_denominator.sum()
    Jc = Xc - Bc * Yc
    return Bc, Jc


def levenberg_marquardt(
        image: sfm.Image,
        data: list[tuple[Tensor, Tensor, Tensor, Tensor]],
        matches_file: loader.MatchesFile,
        betac_init: float,
        gammac_init: float,
        max_iter: int = 200,
        function_tolerance: float = 1e-5,
        device: str = 'cpu'
) -> tuple[Tensor, float, float, float]:
    print(f'Optimize Jc, Bc, betac and gammac in a Levenberg-Marquardt scheme ({max_iter} maximum iterations).')
    betac = torch.tensor(betac_init, dtype=torch.float32, device=device)
    gammac = torch.tensor(gammac_init, dtype=torch.float32, device=device)
    residuals = torch.zeros(len(matches_file), dtype=torch.float32, device=device)
    jacobian = torch.zeros(len(matches_file), 2, dtype=torch.float32, device=device)

    damping = 0.1
    eye = torch.eye(2, dtype=torch.float32, device=device)
    previous_cost = torch.inf

    for iteration in range(max_iter):

        Bc, Jc = compute_Bc_Jc(image=image, data=data, betac=betac, gammac=gammac, device=device)

        cursor = 0
        for ui, vi, zi, Iic in iter_data(data, device=device):
            length = zi.shape[0]
            residuals[cursor: cursor + length] = (
                    Iic - Jc[vi, ui] * torch.exp(-betac * zi) - Bc * (1 - torch.exp(-gammac * zi))
            )
            jacobian[cursor: cursor + length, 0] = (zi * Jc[vi, ui] * torch.exp(-betac * zi))
            jacobian[cursor: cursor + length, 1] = (-zi * Bc * torch.exp(-gammac * zi))
            cursor += length

        delta = torch.inverse(jacobian.T @ jacobian + damping * eye) @ (jacobian.T @ residuals)
        betac -= delta[0]
        gammac -= delta[1]

        cost = torch.square(residuals).sum()

        cost_change = torch.abs(previous_cost - cost) / cost
        print(f'iter: {iteration:04d}, cost: {cost.item():.8e}, cost change: {cost_change.item():.8e}, damping: '
              f'{damping:.8e}, Bc: {Bc.item():.3e}, betac: {betac.item():.3e}, gammac: {gammac.item():.3e}')
        if cost_change < function_tolerance or cost.isnan():
            break
        elif cost < previous_cost:
            damping = max(damping / 10, 1e-32)
        else:
            damping *= 10
        previous_cost = cost

    Bc, Jc = compute_Bc_Jc(image=image, data=data, betac=betac, gammac=gammac, device=device)
    return Jc.cpu(), Bc.item(), betac.item(), gammac.item()


def scipy_minimize(
        image: sfm.Image,
        data: list[tuple[Tensor, Tensor, Tensor, Tensor]],
        matches_file: loader.MatchesFile,
        betac_init: float,
        gammac_init: float,
        solver: str = 'l-bfgs-b',
        max_iter: int = 200,
        device: str = 'cpu'
) -> tuple[Tensor, float, float, float]:
    if solver not in ['l-bfgs-b', 'simplex']:
        raise ValueError("`solver` supports 'l-bfgs-b' or 'simplex'.")
    print(f'Optimize Jc, Bc, betac and gammac with {solver} algorithm ({max_iter} maximum iterations).')

    size = len(matches_file)

    def residuals(x):
        betac_hat, gammac_hat = x.tolist()
        Bc_hat, Jc_hat = compute_Bc_Jc(image=image, data=data, betac=betac_hat, gammac=gammac_hat, device=device)
        cost = torch.zeros(1, dtype=torch.float32, device=device)
        jacobian = torch.zeros(2, dtype=torch.float32, device=device)
        for ui, vi, zi, Iic in iter_data(data, device=device):
            r = Iic - Jc_hat[vi, ui] * torch.exp(-betac_hat * zi) - Bc_hat * (1 - torch.exp(-gammac_hat * zi))
            cost += torch.square(r).sum()
            if solver == 'l-bfgs-b':
                jacobian[0] += torch.sum(2 * r * Jc_hat[vi, ui] * zi * torch.exp(-betac_hat * zi))
                jacobian[1] -= torch.sum(2 * r * Bc_hat * zi * torch.exp(-gammac_hat * zi))
        if solver == 'l-bfgs-b':
            return cost.item() / size, (jacobian / size).tolist()
        return cost.item()

    betac, gammac = minimize(
        residuals,
        np.array([betac_init, gammac_init]),
        method='L-BFGS-B' if solver == 'l-bfgs-b' else 'Nelder-Mead',
        jac=solver == 'l-bfgs-b',
        bounds=[(1e-8, 5), (1e-8, 5)],
        options={'maxiter': max_iter, 'disp': True}
    ).x.tolist()
    Bc, Jc = compute_Bc_Jc(image=image, data=data, betac=betac, gammac=gammac, device=device)

    return Jc.cpu(), Bc.item(), betac, gammac


def adam(
        data: list[tuple[Tensor, Tensor, Tensor, Tensor]],
        matches_file: loader.MatchesFile,
        Jc_init: Tensor,
        Bc_init: float,
        betac_init: float,
        gammac_init: float,
        num_iter: int = 200,
        device: str = 'cpu'
) -> tuple[Tensor, float, float, float]:
    print(f'Optimize Jc, Bc, betac and gammac with Adam optimizer ({num_iter} iterations).')
    Jc = torch.nn.Parameter(Jc_init.to(device))
    Bc = torch.nn.Parameter(torch.tensor(Bc_init, dtype=torch.float32, device=device))
    betac = torch.nn.Parameter(torch.tensor(betac_init, dtype=torch.float32, device=device))
    gammac = torch.nn.Parameter(torch.tensor(gammac_init, dtype=torch.float32, device=device))

    optimizer = torch.optim.Adam([Jc, Bc, betac, gammac], lr=0.01)

    size = len(matches_file)
    previous_cost = np.inf

    for iteration in range(num_iter):

        cost = 0
        optimizer.zero_grad()

        for ui, vi, zi, Iic in iter_data(data, device=device):
            loss = torch.square(
                Iic - Jc[vi, ui] * torch.exp(-betac * zi) - Bc * (1 - torch.exp(-gammac * zi))
            ).sum()
            (loss / size).backward()
            cost += loss.item()

        optimizer.step()
        cost_change = np.abs(previous_cost - cost) / cost

        print(f'iter: {iteration:04d}, cost: {cost:.8e}, cost change: {cost_change:.8e}, '
              f'Bc: {Bc.item():.3e}, betac: {betac.item():.3e}, gammac: {gammac.item():.3e}')
        previous_cost = cost

    return Jc.detach().cpu(), Bc.item(), betac.item(), gammac.item()


def solve_sucre(
        image: sfm.Image,
        channel: int,
        matches_file: loader.MatchesFile,
        init: str = 'fast',
        solver: str = 'lm',
        max_iter: int = 200,
        function_tolerance: float = 1e-5,
        device: str = 'cpu'
) -> tuple[Tensor, float, float, float]:
    data = matches_file.load_channel(channel, pin_memory=device.lower() != 'cpu')
    Jc_init, Bc_init, betac_init, gammac_init = initialize_sucre_parameters(
        image=image, data=data, matches_file=matches_file, channel=channel, mode=init, device=device
    )
    print(f'Bc: {Bc_init}, betac: {betac_init}, gammac: {gammac_init}')

    diverged = False

    # TODO: filter matches by distance
    # TODO: harmonize cost across all solvers

    if max_iter > 0:
        match solver:
            case 'lm':
                Jc, Bc, betac, gammac = levenberg_marquardt(
                    image=image,
                    data=data,
                    matches_file=matches_file,
                    betac_init=betac_init,
                    gammac_init=gammac_init,
                    max_iter=max_iter,
                    function_tolerance=function_tolerance,
                    device=device
                )
            case 'l-bfgs-b' | 'simplex':
                Jc, Bc, betac, gammac = scipy_minimize(
                    image=image,
                    data=data,
                    matches_file=matches_file,
                    betac_init=betac_init,
                    gammac_init=gammac_init,
                    solver=solver,
                    max_iter=max_iter,
                    device=device
                )
            case 'adam':
                Jc, Bc, betac, gammac = adam(
                    data=data,
                    matches_file=matches_file,
                    Jc_init=Jc_init,
                    Bc_init=Bc_init,
                    betac_init=betac_init,
                    gammac_init=gammac_init,
                    num_iter=max_iter,
                    device=device
                )
            case _:
                raise ValueError("`method` supports 'lm', 'l-bfgs-b', 'simplex' or 'adam'")

        if any([np.isnan(Bc), np.isnan(betac), np.isnan(gammac)]):
            print('WARNING: optimization diverged, revert to initial parameters.')
            Jc, Bc, betac, gammac = Jc_init, Bc_init, betac_init, gammac_init
            diverged = True
    else:
        Jc, Bc, betac, gammac = Jc_init, Bc_init, betac_init, gammac_init

    if init == 'fast' and (diverged or max_iter <= 0):
        Bc, Jc = compute_Bc_Jc(image=image, data=data, betac=betac, gammac=gammac, device=device)
        Bc = Bc.item()
        Jc = Jc.cpu()

    print(f'Bc: {Bc}, betac: {betac}, gammac: {gammac}')
    return Jc, Bc, betac, gammac


def sucre(
        colmap_model: sfm.COLMAPModel,
        image_name: str,
        output_dir: Path,
        min_cover: float,
        filter_image_names: list[str] = None,
        init: str = 'fast',
        solver: str = 'lm',
        max_iter: int = 200,
        function_tolerance: float = 1e-5,
        force_compute_matches: bool = False,
        keep_matches: bool = False,
        num_workers: int = 0,
        device: str = 'cpu'
):
    image = colmap_model[image_name]
    image_list = list(colmap_model.images.values())

    # Filter images that should not be used for pairing
    if filter_image_names is not None:
        image_list = [im for im in image_list if im.name not in filter_image_names]

    matches_path = (output_dir / image_name).with_suffix('.h5')
    matches_file = loader.MatchesFile(matches_path, overwrite=force_compute_matches)

    if force_compute_matches or not matches_path.exists():
        print(f'Compute {image_name} matches.')
        image.match_images(
            image_list=image_list,
            matches_file=matches_file,
            min_cover=min_cover,
            num_workers=num_workers,
            device=device
        )
        print('Prepare matches for optimization.')
        matches_file.prepare_matches(colmap_model=colmap_model, num_workers=num_workers, device=device)

    print(f'Total of {len(matches_file)} observations.')

    J = torch.full((image.camera.height, image.camera.width, 3), torch.nan, dtype=torch.float32)
    B = torch.full((3,), torch.nan, dtype=torch.float32)
    beta = torch.full((3,), torch.nan, dtype=torch.float32)
    gamma = torch.full((3,), torch.nan, dtype=torch.float32)
    for channel in range(3):
        print(f'----------------------{["---", "-----", "----"][channel]}---------')
        print(f'SUCRe optimization on {["red", "green", "blue"][channel]} channel.')
        print(f'----------------------{["---", "-----", "----"][channel]}---------')
        J[:, :, channel], B[channel], beta[channel], gamma[channel] = solve_sucre(
            image=image,
            channel=channel,
            matches_file=matches_file,
            init=init,
            solver=solver,
            max_iter=max_iter,
            function_tolerance=function_tolerance,
            device=device
        )

    print('Save restored image.')
    Image.fromarray(
        np.uint8(normalization.histogram_stretching(J) * 255)
    ).save((output_dir / image_name).with_suffix('.png'))
    torch.save(J, (output_dir / image_name).with_suffix('.pt'))
    with open((output_dir / image_name).with_suffix('.yml'), 'w') as yaml_file:
        yaml.dump({'B': B.tolist(), 'beta': beta.tolist(), 'gamma': gamma.tolist()}, yaml_file)

    if not keep_matches:
        print(f'Erase {matches_path}.')
        matches_path.unlink()


def parse_args(args: argparse.Namespace):
    print('Loading COLMAP model.')
    colmap_model = sfm.COLMAPModel(model_dir=args.model_dir, image_dir=args.image_dir, depth_dir=args.depth_dir)

    filter_image_names = args.filter_images_path.read_text().splitlines() if args.filter_images_path else None

    if args.image_name is not None:
        image_names = [args.image_name]
    elif args.image_list is not None:
        image_names = args.image_list.read_text().splitlines()
    else:
        image_names = []
        for image_id in range(args.image_ids[0], args.image_ids[1] + 1):
            if image_id in colmap_model.images:
                image_names.append(colmap_model.images[image_id].name)

    for image_name in image_names:
        sucre(
            colmap_model=colmap_model,
            image_name=image_name,
            output_dir=args.output_dir,
            min_cover=args.min_cover,
            filter_image_names=filter_image_names,
            init=args.initialization,
            solver=args.solver,
            max_iter=args.max_iter,
            function_tolerance=args.function_tolerance,
            force_compute_matches=args.force_compute_matches,
            keep_matches=args.keep_matches,
            num_workers=args.num_workers,
            device=args.device
        )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='SUCRe.',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--image-dir', required=True, type=Path, help='path to images directory.')
    parser.add_argument('--depth-dir', required=True, type=Path, help='path to depth maps directory.')
    parser.add_argument('--model-dir', required=True, type=Path, help='path to undistorted COLMAP model directory.')
    parser.add_argument('--output-dir', required=True, type=Path, help='path to output directory.')
    parser_images = parser.add_mutually_exclusive_group(required=True)
    parser_images.add_argument('--image-name', type=str, help='name of image to restore.')
    parser_images.add_argument('--image-list', type=Path,
                               help='path to .txt file with names of images to restore, one name per line.')
    parser_images.add_argument('--image-ids', type=int, nargs=2, metavar=('MIN_ID', 'MAX_ID'),
                               help='range of ids of images to restore in the COLMAP model [min, max].')
    parser.add_argument('--min-cover', type=float, default=0.01,
                        help='minimum percentile of shared observations to keep the pairs of an image.')
    parser.add_argument('--filter-images-path', type=Path,
                        help='path to a .txt file with names of images to '
                             'discard when computing matches, one name per line.')
    parser.add_argument('--initialization', type=str, choices=['fast', 'dense'], default='dense',
                        help='initialize parameters with Gaussian Sea-thru on one image (fast) or all matches (dense).')
    parser.add_argument('--solver', type=str, choices=['lm', 'l-bfgs-b', 'simplex', 'adam'],
                        default='lm', help='method to solve SUCRe least squares.')
    parser.add_argument('--max-iter', type=int, default=200, help='maximum number of optimization steps.')
    parser.add_argument('--function-tolerance', type=float, default=1e-5,
                        help='stops optimization if cost function change is below this threshold.')
    parser.add_argument('--force-compute-matches', action='store_true',
                        help='if matches file already exist, erase it and recompute matches.')
    parser.add_argument('--keep-matches', action='store_true', help='keep matches file (can take a lot a space).')
    parser.add_argument('--num-workers', type=int, default=0, help='number of threads, 0 is the main thread.')
    parser.add_argument('--device', type=str, default='cpu', help='device for heavy computation.')

    parse_args(parser.parse_args())
