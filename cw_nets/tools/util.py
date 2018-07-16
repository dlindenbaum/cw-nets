# Import base tools
import os
## Note, for mac osx compatability import something from shapely.geometry before importing fiona or geopandas
## https://github.com/Toblerity/Shapely/issues/553  * Import shapely before rasterio or fioana
from shapely import geometry
import rasterio
import random
from cw_tiler import main
from cw_tiler import utils
from cw_tiler import vector_utils
from cw_nets.Ternaus_tools import tn_tools 
import numpy as np
import os
from tqdm import tqdm
import random
import torch
import json
# Setting Certificate Location for Ubuntu/Mac OS locations (Rasterio looks for certs in centos locations)
os.environ['CURL_CA_BUNDLE']='/etc/ssl/certs/ca-certificates.crt'


def get_processing_details(rasterPath, smallExample=False, 
                             dstkwargs={"nodata": 0,
                                        "interleave": "pixel",
                                        "tiled": True,
                                        "blockxsize": 512,
                                        "blockysize": 512,
                                        "compress": "LZW"}):
    with rasterio.open(rasterPath) as src:

        # Get Lat, Lon bounds of the Raster (src)
        wgs_bounds = utils.get_wgs84_bounds(src)

        # Use Lat, Lon location of Image to get UTM Zone/ UTM projection
        utm_crs = utils.calculate_UTM_crs(wgs_bounds)

        # Calculate Raster bounds in UTM coordinates 
        utm_bounds = utils.get_utm_bounds(src, utm_crs)

        vrt_profile = utils.get_utm_vrt_profile(src,
                                               crs=utm_crs,
                                               )


        dst_profile = vrt_profile
        dst_profile.update({'count': 1,
                                'dtype': rasterio.uint8,
                            'driver': "GTiff",

                       })
        # update for CogStandard
        dst_profile.update(dstkwargs)


    # open s3 Location
    rasterBounds = geometry.box(*utm_bounds)

    if smallExample:
        rasterBounds = geometry.box(*rasterBounds.centroid.buffer(1000).bounds)
    
    return rasterBounds, dst_profile

def generate_cells_list_dict(rasterBounds, cell_size_meters, stride_size_meters, tile_size_pixels):
    
    cells_list_dict = main.calculate_analysis_grid(rasterBounds.bounds, 
                                                   stride_size_meters=stride_size_meters, 
                                                   cell_size_meters=cell_size_meters,
                                                  quad_space=True)
    
    return cells_list_dict

def createRasterMask(rasterPath, 
                     cells_list_dict, 
                     dataLocation, 
                     outputName, 
                     dst_profile, 
                     modelPath,
                    tile_size_pixels):
    
    mask_dict_list = []
    model = tn_tools.get_model(modelPath)
    outputTifMask = os.path.join(dataLocation, outputName.replace('.tif', '_mask.tif'))
    outputTifCountour = os.path.join(dataLocation, outputName.replace('.tif', '_contour.tif'))
    outputTifCount = os.path.join(dataLocation, outputName.replace('.tif', '_count.tif'))


    # define Image_transform for Tile
    img_transform = tn_tools.get_img_transform()
    # Open Raster File
    with rasterio.open(rasterPath) as src:

        for cells_list_id, cells_list in cells_list_dict.items():

            outputTifMask = os.path.join(dataLocation, outputName.replace('.tif', '{}_mask.tif'.format(cells_list_id)))
            outputTifCountour = os.path.join(dataLocation, outputName.replace('.tif', '{}_contour.tif'.format(cells_list_id)))
            outputTifCount = os.path.join(dataLocation, outputName.replace('.tif', '{}_count.tif'.format(cells_list_id)))

            # Open Results TIF
            with rasterio.open(outputTifMask,
                                   'w',
                                   **dst_profile) as dst, \
                rasterio.open(outputTifCountour,
                                   'w',
                                   **dst_profile) as dst_countour, \
                rasterio.open(outputTifCount,
                                   'w',
                                   **dst_profile) as dst_count:

                src_profile = src.profile

                print("start interating through {} cells".format(len(cells_list_dict[0])))
                for cell_selection in tqdm(cells_list):
                    # Break up cell into four gorners
                    ll_x, ll_y, ur_x, ur_y = cell_selection


                    # Get Tile from bounding box
                    tile, mask, window, window_transform = main.tile_utm(src, ll_x, ll_y, ur_x, ur_y, indexes=None, tilesize=tile_size_pixels, nodata=None, alpha=None,
                                 dst_crs=dst_profile['crs'])


                    img = tn_tools.reform_tile(tile)
                    img, pads = tn_tools.pad(img)

                    input_img = torch.unsqueeze(img_transform(img / 255).cuda(), dim=0)

                    predictDict = tn_tools.predict(model, input_img, pads)
                    # Returns predictDict = {'mask': mask, # Polygon Results for detection of buildings
                          # 'contour': contour, # Contour results for detecting edge of buildings
                          # 'seed': seed, # Mix of Contour and Mask for used by watershed function
                          # 'labels': labels # Result of watershed function
                        #} 


  
                    dst.write(tn_tools.unpad(predictDict['mask'], pads).astype(np.uint8), window=window, indexes=1)
                    dst_countour.write(tn_tools.unpad(predictDict['seed'], pads).astype(np.uint8), window=window, indexes=1)
                    dst_count.write(np.ones(predictDict['labels'].shape).astype(np.uint8), window=window, indexes=1)
            
            resultDict = {'mask': outputTifMask,
                         'contour': outputTifCountour,
                         'count': outputTifCount}
            
            
            mask_dict_list.append(resultDict)
            
            
    return mask_dict_list
            
def process_results_mask(mask_dict_list, outputNameTiff,  delete_tmp=True):
    firstCell = True
    for resultDict in tqdm(mask_dict_list):
        
        with rasterio.open(resultDict['mask']) as src_mask, \
                rasterio.open(resultDict['contour']) as src_seed, \
                rasterio.open(resultDict['count']) as src_count:
            
            src_mask_profile = src_mask.profile
            
            if firstCell:
                data_mask = src_mask.read()
                data_count = src_count.read()
                firstCell = False
            else:
                data_mask += src_mask.read()
                data_count += src_count.read()
                
    
    data_mask=(data_mask/data_count).astype(np.uint8)
    data_mask=data_mask>=1.0
    
    
    with rasterio.open(outputNameTiff,
                                   'w',
                               **src_mask_profile) as dst:
    
        dst.write(data_mask)
        
    
    resultDict = {'mask': outputNameTiff}
        
    
    return resultDict

def polygonize_results_mask(maskDict):
    
    results = []
    #mask= data_mask==0
    with rasterio.open(maskDict['mask']) as src:
        src_profile = src.profile
        image = src.read(1)
        mask=image>0
        for i, (geom, val) in tqdm(enumerate(rasterio.features.shapes(image, mask=mask, transform=src.transform))):
            geom = rasterio.warp.transform_geom(src.crs, 'EPSG:4326', geom, precision=6)
            results.append({
                "type": "Feature", 
                'properties': {'raster_val': val}, 
                'geometry': geom
            }
                          )
    
        
    return results, src_profile

def write_results_tojson(results, dst_name):
    
    

    collection = {
        'type': 'FeatureCollection', 
        'features': list(results) }

    with open(dst_name, 'w') as dst:
        json.dump(collection, dst)


