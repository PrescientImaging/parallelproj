import numpy as np
import math

import numpy.ctypeslib as npct
import ctypes

from setup_testdata   import setup_testdata
from time import time

#------------------------------------------------------------------------------------------------
#---- parse the command line
import argparse
parser = argparse.ArgumentParser()
parser.add_argument('--nv', type=int, default = 7, help='number of view to project')
args = parser.parse_args()

nviews = args.nv

###############################################################
# wrappers to call functions from compiled libs ###############

ar_1d_single = npct.ndpointer(dtype = np.float32, ndim = 1, flags = 'C')

lib_parallelproj = npct.load_library('libparallelproj.so','../lib')

lib_parallelproj.joseph3d_lm.restype  = None
lib_parallelproj.joseph3d_lm.argtypes = [ar_1d_single,
                                         ar_1d_single,
                                         ar_1d_single,
                                         ar_1d_single,
                                         ar_1d_single,
                                         ar_1d_single,
                                         ctypes.c_ulonglong,
                                         ctypes.c_uint,
                                         ctypes.c_uint,
                                         ctypes.c_uint]

lib_parallelproj.joseph3d_lm_back.restype  = None
lib_parallelproj.joseph3d_lm_back.argtypes = [ar_1d_single,
                                              ar_1d_single,
                                              ar_1d_single,
                                              ar_1d_single,
                                              ar_1d_single,
                                              ar_1d_single,
                                              ctypes.c_ulonglong,
                                              ctypes.c_uint,
                                              ctypes.c_uint,
                                              ctypes.c_uint]
###############################################################
###############################################################



#--------------------------------------------------------------------------------------
#---- set up phantom and dector coordindates

xstart, xend, img, img_origin, voxsize = setup_testdata(nviews = nviews)

# swap axes
# it seems to be best to have (radial, angle, plane) in memory
xstart     = np.swapaxes(np.swapaxes(xstart, 1, 3), 1, 2)
xend       = np.swapaxes(np.swapaxes(xend,   1, 3), 1, 2)

sino_shape = xstart.shape[1:]
print(sino_shape)

# flatten the sinogram coordinates
xstart = xstart.reshape((3,) + (np.prod(sino_shape),)).transpose()
xend   = xend.reshape((3,) + (np.prod(sino_shape),)).transpose()

# forward projection
nLORs   = xstart.shape[0]
img_fwd = np.zeros(nLORs, np.float32)  

n0, n1, n2 = img.shape
 
t0 = time()
ok = lib_parallelproj.joseph3d_lm(xstart.flatten(), xend.flatten(), img.flatten(), 
                                  img_origin, voxsize, img_fwd, nLORs, n0, n1, n2) 

img_fwd_sino = img_fwd.reshape(sino_shape)
t1 = time()
t_fwd = t1 - t0

# back projection
ones = np.ones(nLORs, dtype = np.float32)  
back_img = np.zeros(img.shape, dtype = np.float32).flatten()

t2 = time()
ok = lib_parallelproj.joseph3d_lm_back(xstart.flatten(), xend.flatten(), back_img, 
                                       img_origin, voxsize, ones, nLORs, n0, n1, n2) 
back_img = back_img.reshape((n0,n1,n2))
t3 = time()
t_back = t3 - t2
  
#----
# print results
print('openmp cpu','#views',nviews,'fwd',t_fwd)
print('openmp cpu','#views',nviews,'back',t_back)

## show results
#import pymirc.viewer as pv
#vi = pv.ThreeAxisViewer(img_fwd_sino[:,:,:88])
#vi = pv.ThreeAxisViewer(back_img)
