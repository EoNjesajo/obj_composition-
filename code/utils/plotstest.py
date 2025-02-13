import numpy as np
import torch
from skimage import measure
import torchvision
import trimesh
from PIL import Image
import matplotlib.pyplot as plt
import cv2
import h5py
from tqdm import tqdm

from utils import rend_util
from utils.general import trans_topil
from utils.sem_util import mask2color

import plotly.graph_objs as go
import plotly.offline as offline
from plotly.subplots import make_subplots

import random

def plot(implicit_network, indices, plot_data, path, epoch, img_res, rendering_network, plot_nimgs, resolution, grid_boundary,  level=0):

    if plot_data is not None:
        cam_loc, cam_dir = rend_util.get_camera_for_plot(plot_data['pose'])

        # plot images
        plot_images(plot_data['rgb_eval'], plot_data['rgb_gt'], path, epoch, plot_nimgs, img_res, indices)
      
        # plot normal maps
        plot_normal_maps(plot_data['normal_map'], plot_data['normal_gt'], path, epoch, plot_nimgs, img_res, indices)

        # plot depth maps
        plot_depth_maps(plot_data['depth_map'], plot_data['depth_gt'], path, epoch, plot_nimgs, img_res, indices)

        # plot sem maps
        plot_sem_maps(plot_data['seg_map'], plot_data['seg_gt'], path, epoch, plot_nimgs, img_res, indices)

        # concat output images to single large image
        images = []
        for name in ["rendering", "depth", "normal", "sem"]:
            images.append(cv2.imread('{0}/{1}_{2}_{3}.png'.format(path, name, epoch, indices[0])))        

        images = np.concatenate(images, axis=1)
        cv2.imwrite('{0}/merge_{1}_{2}.png'.format(path, epoch, indices[0]), images)

    # plot each mesh
    sem_num = implicit_network.d_out
    # for indx in range(sem_num):
    _ = get_semantic_surface_trace(path=path,
                                    epoch=epoch,
                                    sdf = lambda x: implicit_network(x)[:, :sem_num],
                                    implicit = implicit_network,
                                    renderer = rendering_network,
                                    resolution=resolution,
                                    grid_boundary=grid_boundary,
                                    level=level,
                                    num = sem_num
                                    )

    # _ = get_surface_trace(path=path,
    #                     epoch=epoch,
    #                     sdf=lambda x: implicit_network.get_sdf_vals(x),
    #                     resolution=512,
    #                     grid_boundary=grid_boundary,
    #                     level=level
    #                     )

def plot_test(implicit_network, path, rendering_network, plot_nimgs, resolution, grid_boundary,  level=0):
    # plot each mesh
    sem_num = implicit_network.d_out
    _ = get_semantic_surface_trace(path=path,
                                    epoch='ckpt',
                                    sdf = lambda x: implicit_network(x)[:, :sem_num],
                                    implicit = implicit_network,
                                    renderer = rendering_network,
                                    resolution=resolution,
                                    grid_boundary=grid_boundary,
                                    level=level,
                                    num = sem_num
                                    )
    _ = get_surface_trace(path=path,
                        epoch='ckpt',
                        sdf=lambda x: implicit_network.get_sdf_vals(x),
                        resolution=512,
                        grid_boundary=grid_boundary,
                        level=level
                        )

avg_pool_3d = torch.nn.AvgPool3d(2, stride=2)
upsample = torch.nn.Upsample(scale_factor=2, mode='nearest')


@torch.no_grad()
def get_surface_sliding(path, epoch, sdf, resolution=100, grid_boundary=[-2.0, 2.0], return_mesh=False, level=0):
    # assert resolution % 512 == 0
    resolution = 1024
    resN = resolution
    cropN = 512
    level = 0
    N = resN // cropN

    grid_min = [grid_boundary[0], grid_boundary[0], grid_boundary[0]]
    grid_max = [grid_boundary[1], grid_boundary[1], grid_boundary[1]]
    xs = np.linspace(grid_min[0], grid_max[0], N+1)
    ys = np.linspace(grid_min[1], grid_max[1], N+1)
    zs = np.linspace(grid_min[2], grid_max[2], N+1)

    print(xs)
    print(ys)
    print(zs)
    meshes = []
    for i in range(N):
        for j in range(N):
            for k in range(N):
                print(i, j, k)
                x_min, x_max = xs[i], xs[i+1]
                y_min, y_max = ys[j], ys[j+1]
                z_min, z_max = zs[k], zs[k+1]

                x = np.linspace(x_min, x_max, cropN)
                y = np.linspace(y_min, y_max, cropN)
                z = np.linspace(z_min, z_max, cropN)

                xx, yy, zz = np.meshgrid(x, y, z, indexing='ij')
                points = torch.tensor(np.vstack([xx.ravel(), yy.ravel(), zz.ravel()]).T, dtype=torch.float).cuda()
                
                def evaluate(points):
                    z = []
                    for _, pnts in enumerate(torch.split(points, 100000, dim=0)):
                        z.append(sdf(pnts))
                    z = torch.cat(z, axis=0)
                    return z
            
                # construct point pyramids
                points = points.reshape(cropN, cropN, cropN, 3).permute(3, 0, 1, 2)
                points_pyramid = [points]
                for _ in range(3):            
                    points = avg_pool_3d(points[None])[0]
                    points_pyramid.append(points)
                points_pyramid = points_pyramid[::-1]
                
                # evalute pyramid with mask
                mask = None
                threshold = 2 * (x_max - x_min)/cropN * 8
                for pid, pts in enumerate(points_pyramid):
                    coarse_N = pts.shape[-1]
                    pts = pts.reshape(3, -1).permute(1, 0).contiguous()
                    
                    if mask is None:    
                        pts_sdf = evaluate(pts)
                    else:                    
                        mask = mask.reshape(-1)
                        pts_to_eval = pts[mask]
                        #import pdb; pdb.set_trace()
                        if pts_to_eval.shape[0] > 0:
                            pts_sdf_eval = evaluate(pts_to_eval.contiguous())
                            pts_sdf[mask] = pts_sdf_eval
                        print("ratio", pts_to_eval.shape[0] / pts.shape[0])

                    if pid < 3:
                        # update mask
                        mask = torch.abs(pts_sdf) < threshold
                        mask = mask.reshape(coarse_N, coarse_N, coarse_N)[None, None]
                        mask = upsample(mask.float()).bool()

                        pts_sdf = pts_sdf.reshape(coarse_N, coarse_N, coarse_N)[None, None]
                        pts_sdf = upsample(pts_sdf)
                        pts_sdf = pts_sdf.reshape(-1)

                    threshold /= 2.

                z = pts_sdf.detach().cpu().numpy()

                if (not (np.min(z) > level or np.max(z) < level)):
                    z = z.astype(np.float32)
                    verts, faces, normals, values = measure.marching_cubes(
                    volume=z.reshape(cropN, cropN, cropN), #.transpose([1, 0, 2]),
                    level=level,
                    spacing=(
                            (x_max - x_min)/(cropN-1),
                            (y_max - y_min)/(cropN-1),
                            (z_max - z_min)/(cropN-1) ))
                    print(np.array([x_min, y_min, z_min]))
                    print(verts.min(), verts.max())
                    verts = verts + np.array([x_min, y_min, z_min])
                    print(verts.min(), verts.max())
                    
                    meshcrop = trimesh.Trimesh(verts, faces, normals)
                    #meshcrop.export(f"{i}_{j}_{k}.ply")
                    meshes.append(meshcrop)

    combined = trimesh.util.concatenate(meshes)

    if return_mesh:
        return combined
    else:
        combined.export('{0}/surface_{1}_whole.ply'.format(path, epoch), 'ply') 



def get_surface_trace(path, epoch, sdf, resolution=100, grid_boundary=[-2.0, 2.0], return_mesh=False, level=0):
    grid = get_grid_uniform(resolution, grid_boundary)
    points = grid['grid_points']

    z = []
    for i, pnts in tqdm(enumerate(torch.split(points, 100000, dim=0))):
        z.append(sdf(pnts.cuda()).detach().cpu().numpy())
    z = np.concatenate(z, axis=0)
    
    if (not (np.min(z) > level or np.max(z) < level)):

        z = z.astype(np.float32)

        verts, faces, normals, values = measure.marching_cubes(
            volume=z.reshape(grid['xyz'][1].shape[0], grid['xyz'][0].shape[0],
                             grid['xyz'][2].shape[0]).transpose([1, 0, 2]),
            level=level,
            spacing=(grid['xyz'][0][2] - grid['xyz'][0][1],
                     grid['xyz'][0][2] - grid['xyz'][0][1],
                     grid['xyz'][0][2] - grid['xyz'][0][1]))

        verts = verts + np.array([grid['xyz'][0][0], grid['xyz'][1][0], grid['xyz'][2][0]])

        I, J, K = faces.transpose()


        traces = [go.Mesh3d(x=verts[:, 0], y=verts[:, 1], z=verts[:, 2],
                            i=I, j=J, k=K, name='implicit_surface',
                            color='#ffffff', opacity=1.0, flatshading=False,
                            lighting=dict(diffuse=1, ambient=0, specular=0),
                            lightposition=dict(x=0, y=0, z=-1), showlegend=True)]


        ## texture
        

        meshexport = trimesh.Trimesh(verts, faces, normals, vertex_colors=values)
        meshexport.export('{0}/surface_{1}_whole.ply'.format(path, epoch), 'ply')
        # print(meshexport)
        
        if return_mesh:
            return meshexport
        return traces
    return None

# @torch.no_grad()
# def get_surface_sliding(path, epoch, sdf, resolution=100, grid_boundary=[-2.0, 2.0], return_mesh=False, level=0):
#     # assert resolution % 512 == 0 or resolution % 256 ==0
#     resolution = 512
#     resN = resolution
#     cropN = 512
#     level = 0
#     N = resN // cropN

#     grid_min = [grid_boundary[0], grid_boundary[0], grid_boundary[0]]
#     grid_max = [grid_boundary[1], grid_boundary[1], grid_boundary[1]]
#     xs = np.linspace(grid_min[0], grid_max[0], N+1)
#     ys = np.linspace(grid_min[1], grid_max[1], N+1)
#     zs = np.linspace(grid_min[2], grid_max[2], N+1)

#     print(xs)
#     print(ys)
#     print(zs)
#     meshes = []
#     for i in range(N):
#         for j in range(N):
#             for k in range(N):
#                 print(i, j, k)
#                 x_min, x_max = xs[i], xs[i+1]
#                 y_min, y_max = ys[j], ys[j+1]
#                 z_min, z_max = zs[k], zs[k+1]

#                 x = np.linspace(x_min, x_max, cropN)
#                 y = np.linspace(y_min, y_max, cropN)
#                 z = np.linspace(z_min, z_max, cropN)

#                 xx, yy, zz = np.meshgrid(x, y, z, indexing='ij')
#                 points = torch.tensor(np.vstack([xx.ravel(), yy.ravel(), zz.ravel()]).T, dtype=torch.float).cuda()
                
#                 def evaluate(points):
#                     z = []
#                     for _, pnts in enumerate(torch.split(points, 100000, dim=0)):
#                         z.append(sdf(pnts))
#                     z = torch.cat(z, axis=0)
#                     return z
            
#                 # construct point pyramids
#                 points = points.reshape(cropN, cropN, cropN, 3).permute(3, 0, 1, 2)
#                 points_pyramid = [points]
#                 for _ in range(3):            
#                     points = avg_pool_3d(points[None])[0]
#                     points_pyramid.append(points)
#                 points_pyramid = points_pyramid[::-1]
                
#                 # evalute pyramid with mask
#                 mask = None
#                 threshold = 2 * (x_max - x_min)/cropN * 8
#                 for pid, pts in enumerate(points_pyramid):
#                     coarse_N = pts.shape[-1]
#                     pts = pts.reshape(3, -1).permute(1, 0).contiguous()
                    
#                     if mask is None:    
#                         pts_sdf = evaluate(pts)
#                     else:                    
#                         mask = mask.reshape(-1)
#                         pts_to_eval = pts[mask]
#                         #import pdb; pdb.set_trace()
#                         if pts_to_eval.shape[0] > 0:
#                             pts_sdf_eval = evaluate(pts_to_eval.contiguous())
#                             pts_sdf[mask] = pts_sdf_eval
#                         print("ratio", pts_to_eval.shape[0] / pts.shape[0])

#                     if pid < 3:
#                         # update mask
#                         mask = torch.abs(pts_sdf) < threshold
#                         mask = mask.reshape(coarse_N, coarse_N, coarse_N)[None, None]
#                         mask = upsample(mask.float()).bool()

#                         pts_sdf = pts_sdf.reshape(coarse_N, coarse_N, coarse_N)[None, None]
#                         pts_sdf = upsample(pts_sdf)
#                         pts_sdf = pts_sdf.reshape(-1)

#                     threshold /= 2.

#                 z = pts_sdf.detach().cpu().numpy()

#                 if (not (np.min(z) > level or np.max(z) < level)):
#                     z = z.astype(np.float32)
#                     verts, faces, normals, values = measure.marching_cubes(
#                     volume=z.reshape(cropN, cropN, cropN), #.transpose([1, 0, 2]),
#                     level=level,
#                     spacing=(
#                             (x_max - x_min)/(cropN-1),
#                             (y_max - y_min)/(cropN-1),
#                             (z_max - z_min)/(cropN-1) ))
#                     print(np.array([x_min, y_min, z_min]))
#                     print(verts.min(), verts.max())
#                     verts = verts + np.array([x_min, y_min, z_min])
#                     print(verts.min(), verts.max())
                    
#                     meshcrop = trimesh.Trimesh(verts, faces, normals)
#                     #meshcrop.export(f"{i}_{j}_{k}.ply")
#                     meshes.append(meshcrop)

#     combined = trimesh.util.concatenate(meshes)

#     if return_mesh:
#         return combined
#     else:
#         combined.export('{0}/surface_{1}_whole.ply'.format(path, epoch), 'ply')    

def get_semantic_surface_trace(path, epoch, sdf, implicit, renderer ,resolution=100, grid_boundary=[-2.0, 2.0], return_mesh=False, level=0, num=0): # default resolution 256 -> 64
    grid = get_grid_uniform(resolution, grid_boundary)
    points = grid['grid_points']
    print(points.shape)
    

    z_all = []
    for i, pnts in tqdm(enumerate(torch.split(points, 100000, dim=0))):
        z_all.append(sdf(pnts.cuda()).detach().cpu().numpy())
    # print("end")
    z_all = np.concatenate(z_all, axis=0)
    # print("seg")

    for idx in tqdm(range(num)):
        # file_name = '{0}/surface_{1}_{2}'.format(path, epoch, idx)
        # file_name = '{0}/{1}_sdf'.format(path, str(idx).zfill(6))
        z = z_all[:, idx]
        
        
        if (not (np.min(z) > level or np.max(z) < level)):

            # samplesdf = z.reshape(len(z), 1)
            # ori_verts = np.asarray([0.0,0.0,0.0], dtype=np.float32).reshape((1,3))
            # norm_params = np.asarray([0.0 ,0.0 ,0.0 ,0.0], dtype=np.float32)
            # sdf_dict = np.asarray([0.0 ,0.0 ,0.0 ,0.0 ,0.0 ,0.0], dtype=np.float32)
            # f1 = h5py.File(file_name + '.h5', 'w')
            # f1.create_dataset('pc_sdf_original', data=ori_verts.astype(np.float32), compression='gzip', compression_opts=4)
            # f1.create_dataset('pc_sdf_sample', data=samplesdf.astype(np.float32), compression='gzip', compression_opts=4)
            # f1.create_dataset('norm_params', data=norm_params, compression='gzip', compression_opts=4)
            # f1.create_dataset('sdf_params', data=sdf_dict, compression='gzip', compression_opts=4)
            # f1.close()


            z = z.astype(np.float32)

            verts, faces, normals, values = measure.marching_cubes(
                volume=z.reshape(grid['xyz'][1].shape[0], grid['xyz'][0].shape[0],
                                grid['xyz'][2].shape[0]).transpose([1, 0, 2]),
                level=level,
                spacing=(grid['xyz'][0][2] - grid['xyz'][0][1],
                        grid['xyz'][0][2] - grid['xyz'][0][1],
                        grid['xyz'][0][2] - grid['xyz'][0][1]))

            verts = verts + np.array([grid['xyz'][0][0], grid['xyz'][1][0], grid['xyz'][2][0]])
            tensor_verts = torch.as_tensor(verts).cuda().type(torch.float32)
        
            ### vertex color part
            _, feature_vectors, gradients, _, _ = implicit.get_outputs(tensor_verts, beta=None)
            dirs_flat = [[0,0,0] for _ in range(len(verts))]
            dirs_flat = torch.as_tensor(dirs_flat).cuda().type(torch.float32)
    
            rgb_flat = renderer(tensor_verts, gradients, dirs_flat, feature_vectors, 0)
            rgb = rgb_flat*255
            rgb = rgb.type(torch.int16).detach().cpu().numpy()
            
            I, J, K = faces.transpose()

            traces = [go.Mesh3d(x=verts[:, 0], y=verts[:, 1], z=verts[:, 2],
                                i=I, j=J, k=K, name='implicit_surface',
                                color='#ffffff', opacity=1.0, flatshading=False,
                                lighting=dict(diffuse=1, ambient=0, specular=0),
                                lightposition=dict(x=0, y=0, z=-1), showlegend=True)]

            meshexport = trimesh.Trimesh(verts, faces, normals, vertex_colors=rgb)
            meshexport.export('{0}/surface_{1}_{2}.ply'.format(path, epoch, idx), 'ply')


    if return_mesh:
        return meshexport
    return traces
    # return None
    
        
def get_3D_scatter_trace(points, name='', size=3, caption=None):
    assert points.shape[1] == 3, "3d scatter plot input points are not correctely shaped "
    assert len(points.shape) == 2, "3d scatter plot input points are not correctely shaped "

    trace = go.Scatter3d(
        x=points[:, 0].cpu(),
        y=points[:, 1].cpu(),
        z=points[:, 2].cpu(),
        mode='markers',
        name=name,
        marker=dict(
            size=size,
            line=dict(
                width=2,
            ),
            opacity=1.0,
        ), text=caption)

    return trace


def get_3D_quiver_trace(points, directions, color='#bd1540', name=''):
    assert points.shape[1] == 3, "3d cone plot input points are not correctely shaped "
    assert len(points.shape) == 2, "3d cone plot input points are not correctely shaped "
    assert directions.shape[1] == 3, "3d cone plot input directions are not correctely shaped "
    assert len(directions.shape) == 2, "3d cone plot input directions are not correctely shaped "

    trace = go.Cone(
        name=name,
        x=points[:, 0].cpu(),
        y=points[:, 1].cpu(),
        z=points[:, 2].cpu(),
        u=directions[:, 0].cpu(),
        v=directions[:, 1].cpu(),
        w=directions[:, 2].cpu(),
        sizemode='absolute',
        sizeref=0.125,
        showscale=False,
        colorscale=[[0, color], [1, color]],
        anchor="tail"
    )

    return trace


# def get_surface_trace(path, epoch, sdf, resolution=100, grid_boundary=[-2.0, 2.0], return_mesh=False, level=0):
#     grid = get_grid_uniform(resolution, grid_boundary)
#     points = grid['grid_points']

#     z = []
#     for i, pnts in enumerate(torch.split(points, 100000, dim=0)):
#         z.append(sdf(pnts.cuda()).detach().cpu().numpy())
#     z = np.concatenate(z, axis=0)

#     if (not (np.min(z) > level or np.max(z) < level)):

#         z = z.astype(np.float32)

#         verts, faces, normals, values = measure.marching_cubes(
#             volume=z.reshape(grid['xyz'][1].shape[0], grid['xyz'][0].shape[0],
#                              grid['xyz'][2].shape[0]).transpose([1, 0, 2]),
#             level=level,
#             spacing=(grid['xyz'][0][2] - grid['xyz'][0][1],
#                      grid['xyz'][0][2] - grid['xyz'][0][1],
#                      grid['xyz'][0][2] - grid['xyz'][0][1]))

#         verts = verts + np.array([grid['xyz'][0][0], grid['xyz'][1][0], grid['xyz'][2][0]])
#         '''
#         I, J, K = faces.transpose()

#         traces = [go.Mesh3d(x=verts[:, 0], y=verts[:, 1], z=verts[:, 2],
#                             i=I, j=J, k=K, name='implicit_surface',
#                             color='#ffffff', opacity=1.0, flatshading=False,
#                             lighting=dict(diffuse=1, ambient=0, specular=0),
#                             lightposition=dict(x=0, y=0, z=-1), showlegend=True)]
#         '''
#         meshexport = trimesh.Trimesh(verts, faces, normals)
#         meshexport.export('{0}/surface_{1}.ply'.format(path, epoch), 'ply')

#         if return_mesh:
#             return meshexport
#         #return traces
#     return None

def get_surface_high_res_mesh(sdf, resolution=100, grid_boundary=[-2.0, 2.0], level=0, take_components=True):
    # get low res mesh to sample point cloud
    grid = get_grid_uniform(100, grid_boundary)
    z = []
    points = grid['grid_points']

    for i, pnts in enumerate(torch.split(points, 100000, dim=0)):
        z.append(sdf(pnts).detach().cpu().numpy())
    z = np.concatenate(z, axis=0)

    z = z.astype(np.float32)
 
    verts, faces, normals, values = measure.marching_cubes(
        volume=z.reshape(grid['xyz'][1].shape[0], grid['xyz'][0].shape[0],
                         grid['xyz'][2].shape[0]).transpose([1, 0, 2]),
        level=level,
        spacing=(grid['xyz'][0][2] - grid['xyz'][0][1],
                 grid['xyz'][0][2] - grid['xyz'][0][1],
                 grid['xyz'][0][2] - grid['xyz'][0][1]))

    verts = verts + np.array([grid['xyz'][0][0], grid['xyz'][1][0], grid['xyz'][2][0]])

    mesh_low_res = trimesh.Trimesh(verts, faces, normals)
    if take_components:
        components = mesh_low_res.split(only_watertight=False)
        areas = np.array([c.area for c in components], dtype=np.float)
        mesh_low_res = components[areas.argmax()]

    recon_pc = trimesh.sample.sample_surface(mesh_low_res, 10000)[0]
    recon_pc = torch.from_numpy(recon_pc).float().cuda()

    # Center and align the recon pc
    s_mean = recon_pc.mean(dim=0)
    s_cov = recon_pc - s_mean
    s_cov = torch.mm(s_cov.transpose(0, 1), s_cov)
    vecs = torch.view_as_real(torch.linalg.eig(s_cov)[1].transpose(0, 1))[:, :, 0]
    if torch.det(vecs) < 0:
        vecs = torch.mm(torch.tensor([[1, 0, 0], [0, 0, 1], [0, 1, 0]]).cuda().float(), vecs)
    helper = torch.bmm(vecs.unsqueeze(0).repeat(recon_pc.shape[0], 1, 1),
                       (recon_pc - s_mean).unsqueeze(-1)).squeeze()

    grid_aligned = get_grid(helper.cpu(), resolution)

    grid_points = grid_aligned['grid_points']

    g = []
    for i, pnts in enumerate(torch.split(grid_points, 100000, dim=0)):
        g.append(torch.bmm(vecs.unsqueeze(0).repeat(pnts.shape[0], 1, 1).transpose(1, 2),
                           pnts.unsqueeze(-1)).squeeze() + s_mean)
    grid_points = torch.cat(g, dim=0)

    # MC to new grid
    points = grid_points
    z = []
    for i, pnts in enumerate(torch.split(points, 100000, dim=0)):
        z.append(sdf(pnts).detach().cpu().numpy())
    z = np.concatenate(z, axis=0)

    meshexport = None
    if (not (np.min(z) > level or np.max(z) < level)):

        z = z.astype(np.float32)

        verts, faces, normals, values = measure.marching_cubes(
            volume=z.reshape(grid_aligned['xyz'][1].shape[0], grid_aligned['xyz'][0].shape[0],
                             grid_aligned['xyz'][2].shape[0]).transpose([1, 0, 2]),
            level=level,
            spacing=(grid_aligned['xyz'][0][2] - grid_aligned['xyz'][0][1],
                     grid_aligned['xyz'][0][2] - grid_aligned['xyz'][0][1],
                     grid_aligned['xyz'][0][2] - grid_aligned['xyz'][0][1]))

        verts = torch.from_numpy(verts).cuda().float()
        verts = torch.bmm(vecs.unsqueeze(0).repeat(verts.shape[0], 1, 1).transpose(1, 2),
                   verts.unsqueeze(-1)).squeeze()
        verts = (verts + grid_points[0]).cpu().numpy()

        
        meshexport = trimesh.Trimesh(verts, faces, normals)

    return meshexport


def get_surface_by_grid(grid_params, sdf, resolution=100, level=0, higher_res=False):
    grid_params = grid_params * [[1.5], [1.0]]

    # params = PLOT_DICT[scan_id]
    input_min = torch.tensor(grid_params[0]).float()
    input_max = torch.tensor(grid_params[1]).float()

    if higher_res:
        # get low res mesh to sample point cloud
        grid = get_grid(None, 100, input_min=input_min, input_max=input_max, eps=0.0)
        z = []
        points = grid['grid_points']

        for i, pnts in enumerate(torch.split(points, 100000, dim=0)):
            z.append(sdf(pnts).detach().cpu().numpy())
        z = np.concatenate(z, axis=0)

        z = z.astype(np.float32)

        verts, faces, normals, values = measure.marching_cubes(
            volume=z.reshape(grid['xyz'][1].shape[0], grid['xyz'][0].shape[0],
                             grid['xyz'][2].shape[0]).transpose([1, 0, 2]),
            level=level,
            spacing=(grid['xyz'][0][2] - grid['xyz'][0][1],
                     grid['xyz'][0][2] - grid['xyz'][0][1],
                     grid['xyz'][0][2] - grid['xyz'][0][1]))

        verts = verts + np.array([grid['xyz'][0][0], grid['xyz'][1][0], grid['xyz'][2][0]])

        mesh_low_res = trimesh.Trimesh(verts, faces, normals)
        components = mesh_low_res.split(only_watertight=False)
        areas = np.array([c.area for c in components], dtype=np.float)
        mesh_low_res = components[areas.argmax()]

        recon_pc = trimesh.sample.sample_surface(mesh_low_res, 10000)[0]
        recon_pc = torch.from_numpy(recon_pc).float().cuda()

        # Center and align the recon pc
        s_mean = recon_pc.mean(dim=0)
        s_cov = recon_pc - s_mean
        s_cov = torch.mm(s_cov.transpose(0, 1), s_cov)
        vecs = torch.view_as_real(torch.linalg.eig(s_cov)[1].transpose(0, 1))[:, :, 0]
        if torch.det(vecs) < 0:
            vecs = torch.mm(torch.tensor([[1, 0, 0], [0, 0, 1], [0, 1, 0]]).cuda().float(), vecs)
        helper = torch.bmm(vecs.unsqueeze(0).repeat(recon_pc.shape[0], 1, 1),
                           (recon_pc - s_mean).unsqueeze(-1)).squeeze()

        grid_aligned = get_grid(helper.cpu(), resolution, eps=0.01)
    else:
        grid_aligned = get_grid(None, resolution, input_min=input_min, input_max=input_max, eps=0.0)

    grid_points = grid_aligned['grid_points']

    if higher_res:
        g = []
        for i, pnts in enumerate(torch.split(grid_points, 100000, dim=0)):
            g.append(torch.bmm(vecs.unsqueeze(0).repeat(pnts.shape[0], 1, 1).transpose(1, 2),
                               pnts.unsqueeze(-1)).squeeze() + s_mean)
        grid_points = torch.cat(g, dim=0)

    # MC to new grid
    points = grid_points
    z = []
    for i, pnts in enumerate(torch.split(points, 100000, dim=0)):
        z.append(sdf(pnts).detach().cpu().numpy())
    z = np.concatenate(z, axis=0)

    meshexport = None
    if (not (np.min(z) > level or np.max(z) < level)):

        z = z.astype(np.float32)

        verts, faces, normals, values = measure.marching_cubes(
            volume=z.reshape(grid_aligned['xyz'][1].shape[0], grid_aligned['xyz'][0].shape[0],
                             grid_aligned['xyz'][2].shape[0]).transpose([1, 0, 2]),
            level=level,
            spacing=(grid_aligned['xyz'][0][2] - grid_aligned['xyz'][0][1],
                     grid_aligned['xyz'][0][2] - grid_aligned['xyz'][0][1],
                     grid_aligned['xyz'][0][2] - grid_aligned['xyz'][0][1]))

        if higher_res:
            verts = torch.from_numpy(verts).cuda().float()
            verts = torch.bmm(vecs.unsqueeze(0).repeat(verts.shape[0], 1, 1).transpose(1, 2),
                       verts.unsqueeze(-1)).squeeze()
            verts = (verts + grid_points[0]).cpu().numpy()
        else:
            verts = verts + np.array([grid_aligned['xyz'][0][0], grid_aligned['xyz'][1][0], grid_aligned['xyz'][2][0]])

        meshexport = trimesh.Trimesh(verts, faces, normals)

        # CUTTING MESH ACCORDING TO THE BOUNDING BOX
        if higher_res:
            bb = grid_params
            transformation = np.eye(4)
            transformation[:3, 3] = (bb[1,:] + bb[0,:])/2.
            bounding_box = trimesh.creation.box(extents=bb[1,:] - bb[0,:], transform=transformation)

            meshexport = meshexport.slice_plane(bounding_box.facets_origin, -bounding_box.facets_normal)

    return meshexport

def get_grid_uniform(resolution, grid_boundary=[-2.0, 2.0]):
    x = np.linspace(grid_boundary[0], grid_boundary[1], resolution)
    y = x
    z = x

    xx, yy, zz = np.meshgrid(x, y, z)
    grid_points = torch.tensor(np.vstack([xx.ravel(), yy.ravel(), zz.ravel()]).T, dtype=torch.float)

    return {"grid_points": grid_points,
            "shortest_axis_length": 2.0,
            "xyz": [x, y, z],
            "shortest_axis_index": 0}

def get_grid(points, resolution, input_min=None, input_max=None, eps=0.1):
    if input_min is None or input_max is None:
        input_min = torch.min(points, dim=0)[0].squeeze().numpy()
        input_max = torch.max(points, dim=0)[0].squeeze().numpy()

    bounding_box = input_max - input_min
    shortest_axis = np.argmin(bounding_box)
    if (shortest_axis == 0):
        x = np.linspace(input_min[shortest_axis] - eps,
                        input_max[shortest_axis] + eps, resolution)
        length = np.max(x) - np.min(x)
        y = np.arange(input_min[1] - eps, input_max[1] + length / (x.shape[0] - 1) + eps, length / (x.shape[0] - 1))
        z = np.arange(input_min[2] - eps, input_max[2] + length / (x.shape[0] - 1) + eps, length / (x.shape[0] - 1))
    elif (shortest_axis == 1):
        y = np.linspace(input_min[shortest_axis] - eps,
                        input_max[shortest_axis] + eps, resolution)
        length = np.max(y) - np.min(y)
        x = np.arange(input_min[0] - eps, input_max[0] + length / (y.shape[0] - 1) + eps, length / (y.shape[0] - 1))
        z = np.arange(input_min[2] - eps, input_max[2] + length / (y.shape[0] - 1) + eps, length / (y.shape[0] - 1))
    elif (shortest_axis == 2):
        z = np.linspace(input_min[shortest_axis] - eps,
                        input_max[shortest_axis] + eps, resolution)
        length = np.max(z) - np.min(z)
        x = np.arange(input_min[0] - eps, input_max[0] + length / (z.shape[0] - 1) + eps, length / (z.shape[0] - 1))
        y = np.arange(input_min[1] - eps, input_max[1] + length / (z.shape[0] - 1) + eps, length / (z.shape[0] - 1))

    xx, yy, zz = np.meshgrid(x, y, z)
    grid_points = torch.tensor(np.vstack([xx.ravel(), yy.ravel(), zz.ravel()]).T, dtype=torch.float).cuda()
    return {"grid_points": grid_points,
            "shortest_axis_length": length,
            "xyz": [x, y, z],
            "shortest_axis_index": shortest_axis}


def plot_normal_maps(normal_maps, ground_true, path, epoch, plot_nrow, img_res, indices):
    ground_true = ground_true.cuda()
    normal_maps = torch.cat((normal_maps, ground_true), dim=0)
    normal_maps_plot = lin2img(normal_maps, img_res)

    tensor = torchvision.utils.make_grid(normal_maps_plot,
                                         scale_each=False,
                                         normalize=False,
                                         nrow=plot_nrow).cpu().detach().numpy()
    tensor = tensor.transpose(1, 2, 0)
    scale_factor = 255
    tensor = (tensor * scale_factor).astype(np.uint8)

    img = Image.fromarray(tensor)
    img.save('{0}/normal_{1}_{2}.png'.format(path, epoch, indices[0]))

    #import pdb; pdb.set_trace()
    #trans_topil(normal_maps_plot[0, :, :, 260:260+680]).save('{0}/2normal_{1}.png'.format(path, epoch))


def plot_images(rgb_points, ground_true, path, epoch, plot_nrow, img_res, indices, exposure=False):
    ground_true = ground_true.cuda()

    output_vs_gt = torch.cat((rgb_points, ground_true), dim=0)
    output_vs_gt_plot = lin2img(output_vs_gt, img_res)

    tensor = torchvision.utils.make_grid(output_vs_gt_plot,
                                         scale_each=False,
                                         normalize=False,
                                         nrow=plot_nrow).cpu().detach().numpy()

    tensor = tensor.transpose(1, 2, 0)
    scale_factor = 255
    tensor = (tensor * scale_factor).astype(np.uint8)

    img = Image.fromarray(tensor)
    if exposure:
        img.save('{0}/exposure_{1}_{2}.png'.format(path, epoch, indices[0]))
    else:
        img.save('{0}/rendering_{1}_{2}.png'.format(path, epoch, indices[0]))


def colored_data(x, cmap='jet', d_min=None, d_max=None):
    if d_min is None:
        d_min = np.min(x)
    if d_max is None:
        d_max = np.max(x)
    x_relative = (x - d_min) / (d_max - d_min)
    cmap_ = plt.cm.get_cmap(cmap)
    return (255 * cmap_(x_relative)[:,:,:3]).astype(np.uint8) # H, W, C

def plot_sem_maps(sem_maps, ground_true, path, epoch, plot_nrow, img_res, indices):
    ground_true = ground_true.cuda()
    # import pdb; pdb.set_trace()
    sem_maps = torch.cat((sem_maps[..., None], ground_true), dim=0)
    sem_maps_plot = lin2img(sem_maps, img_res)
    # sem_maps_plot = mask2color(sem_maps_plot, is_argmax=False)

    tensor = torchvision.utils.make_grid(sem_maps_plot,
                                         scale_each=False,
                                         normalize=False,
                                         nrow=plot_nrow).cpu().detach().numpy()
    tensor = tensor.transpose(1, 2, 0)[:,:,0]
    # import pdb; pdb.set_trace()
    tensor = colored_data(tensor)

    img = Image.fromarray(tensor)
    img.save('{0}/sem_{1}_{2}.png'.format(path, epoch, indices[0]))

    

def plot_depth_maps(depth_maps, ground_true, path, epoch, plot_nrow, img_res, indices):
    ground_true = ground_true.cuda()
    depth_maps = torch.cat((depth_maps[..., None], ground_true), dim=0)
    depth_maps_plot = lin2img(depth_maps, img_res)
    depth_maps_plot = depth_maps_plot.expand(-1, 3, -1, -1)

    tensor = torchvision.utils.make_grid(depth_maps_plot,
                                         scale_each=False,
                                         normalize=False,
                                         nrow=plot_nrow).cpu().detach().numpy()
    tensor = tensor.transpose(1, 2, 0)
    
    save_path = '{0}/depth_{1}_{2}.png'.format(path, epoch, indices[0])
    
    plt.imsave(save_path, tensor[:, :, 0], cmap='viridis')
    

def lin2img(tensor, img_res):
    batch_size, num_samples, channels = tensor.shape
    return tensor.permute(0, 2, 1).view(batch_size, channels, img_res[0], img_res[1])
