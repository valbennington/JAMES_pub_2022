# libraries
import os
from pathlib import Path
from collections import defaultdict
import scipy
import random
import numpy as np
import xarray as xr
import pandas as pd
import joblib
import sklearn
from skimage.filters import sobel
from sklearn.model_selection import train_test_split, GridSearchCV
from sklearn.metrics import r2_score, max_error, mean_squared_error, mean_absolute_error, median_absolute_error
import keras
from keras import Sequential, regularizers
from keras.layers import Dense, BatchNormalization, Dropout
from statsmodels.nonparametric.smoothers_lowess import lowess
#===============================================
# Masks
#===============================================

def network_mask():
    '''network_mask
    This masks out regions in the 
    NCEP land-sea mask (https://www.esrl.noaa.gov/psd/data/gridded/data.ncep.reanalysis.surface.html)
    to define the open ocean. Regions removed include:
    - Coast : defined by sobel filter
    - Bathymetry less than 100m
    - Arctic ocean : defined as North of 79N
    - Hudson Bay
    - caspian sea, black sea, mediterranean sea, baltic sea, Java sea, Red sea
    '''
    ### Load obs directory
    dir_obs = '/local/data/artemis/observations'
    
    ### topography
    ds_topo = xr.open_dataset(f'{dir_obs}/GEBCO_2014/processed/GEBCO_2014_1x1_global.nc')
    ds_topo['lon'] = list(map(lambda x: 360+x if x<0 else x, ds_topo['lon'].values))
    ds_topo = ds_topo.sortby('lon')

    #ds_topo = ds_topo.roll(lon=180, roll_coords='lon')
    #ds_topo['lon'] = np.arange(0.5, 360, 1)

    ### Loads grids
    # land-sea mask
    # land=0, sea=1
    ds_lsmask = xr.open_dataset(f'{dir_obs}/masks/originals/lsmask.nc').sortby('lat').squeeze().drop('time')
    #ds_lsmask['lon'] = list(map(lambda x: x-360 if x>180 else x, ds_lsmask['lon'].values))
    # Sort by longitude
    #ds_lsmask = ds_lsmask.sortby('lon')
    data = ds_lsmask['mask'].where(ds_lsmask['mask']==1)
    ### Define Latitude and Longitude
    lon = ds_lsmask['lon']
    lat = ds_lsmask['lat']

    ### Remove coastal points, defined by sobel edge detection
    coast = (sobel(ds_lsmask['mask'])>0)
    data = data.where(coast==0)
    
    ### Remove shallow sea, less than 100m
    ### This picks out the Solomon islands and Somoa
    data = data.where(ds_topo['Height']<-100)
    
    ### remove arctic
    data = data.where(~((lat>79)))
    data = data.where(~((lat>67) & (lat<80) & (lon>20) & (lon<180)))
    data = data.where(~((lat>67) & (lat<80) & (lon>-180+360) & (lon<-100+360)))

    ### remove caspian sea, black sea, mediterranean sea, and baltic sea
    data = data.where(~((lat>24) & (lat<70) & (lon>14) & (lon<70)))
    
    ### remove hudson bay
    data = data.where(~((lat>50) & (lat<70) & (lon>-100+360) & (lon<-70+360)))
    data = data.where(~((lat>70) & (lat<80) & (lon>-130+360) & (lon<-80+360)))
    
    ### Remove Red sea
    data = data.where(~((lat>10) & (lat<25) & (lon>10) & (lon<45)))
    data = data.where(~((lat>20) & (lat<50) & (lon>0) & (lon<20)))
    
    # Roll to longitude in SOCAT data #
    data['lon'] = list(map(lambda x: x-360 if x>180 else x, data['lon'].values))
    # Sort by longitude
    data = data.sortby('lon')
    
    return data

def load_socat_mask():
    '''
    load a mask of SOCAT data product
    '''
    # location of masked data
    #dir_mask = '/local/data/artemis/observations/masks/processed'
    
    
    # load data with xarray
    #ds_mask = xr.open_dataset(f'{dir_mask}/SOCATv2019_mask_1x1_198201-201512.nc')
    ds_mask = xr.open_dataset('/data/artemis/workspace/vbennington/SOCAT/processed/dpCO2_WVCorr_monthly_197901-201912.nc')  
    return ds_mask

#===============================================
# Data prep functions
#===============================================

def detrend_time(array_1d, N_time):
    """
        Input: 1d array and the length the time dimension should be
        Output: 1d array of original data less linear trend over time; any location with at least one nan is returned as nan for all times
        Method: assumes 2d array can be filled in column-wise (i.e. time was the first dimension that generated the 1d array)
    """
    array_2d = array_1d.reshape(N_time,-1,order='C')
    nan_mask = (np.any(np.isnan(array_2d), axis=0))
    X = np.arange( N_time )
    regressions = np.polyfit(X, array_2d[:,~nan_mask], 1)
    lin_fit = (np.expand_dims(X,1) * regressions[0:1,:] + regressions[1:,:])
    array_detrend_2d = np.empty(shape=array_2d.shape)
    array_detrend_2d[:] = np.nan
    array_detrend_2d[:,~nan_mask] = array_2d[:,~nan_mask] - lin_fit
    
    return array_detrend_2d.flatten(order='C')

def calc_anom(array_1d, N_time, N_batch, array_mask0=None):
    """
        Input: 1d array, the length the time dimension should be, and the window for averaging
        Output: 1d array of original data less mean during that time period
        Method: assumes 2d array can be filled in C order (i.e. time was the first dimension that generated the 1d array)
        Note: can include an extra array to use to adjust for values that should be set to 0
    """
    array_2d = array_1d.copy()
    if array_mask0 is not None:
        nan_mask = np.isnan(array_2d)
        mask0 = np.nan_to_num(array_mask0, nan=-1.0) <= 0
        array_2d[mask0] = np.nan
    array_2d = array_2d.reshape(N_time,-1,order='C')

    for i in range(-(-N_time//N_batch)):
        avg_val = np.nanmean(array_2d[(i*N_batch):((i+1)*N_batch),:])
        array_2d[(i*N_batch):((i+1)*N_batch),:] = array_2d[(i*N_batch):((i+1)*N_batch),:] - avg_val
    
    output = array_2d.flatten(order='C')
    if array_mask0 is not None:
        output[~nan_mask & mask0] = 0
    
    return output

################################################
# Calculate anoms from a mean seasonal cycle:
################################################
def calc_interannual_anom(df):
    
    # chl, sst, sss, xco2 all may have seasonal cycles in them
    DS = df.to_xarray() # get from multi-index back to an xarray for calculating mean seasonal cycle:
    DS_cycle = DS.groupby("time.month").mean("time")
    
    # Now get anomalies from this mean seasonal cycle:
    DS_anom = DS.groupby("time.month") - DS_cycle
    DS2 = xr.Dataset(
        {
        'anom':(['time','xlon','ylat'], DS_anom                       
        )},

        coords={
        'time': (['time'],DS.time),
        'ylat': (['ylat'],DS.ylat),
        'xlon': (['xlon'],DS.xlon)
        })
        
    df_anom = DS2.to_dataframe()

    return df_anom

###########################################################
def log_or_0(array_1d):
    """
        Input: 1d array
        Output: log of 1d array or 0 for values <=0
    """
    output_ma, output = array_1d.copy(), array_1d.copy()
    output_ma = np.ma.masked_array(output_ma, np.isnan(output_ma))
    output_ma = np.ma.log10(output_ma)
    output[~output_ma.mask] = output_ma[~output_ma.mask]
    output[output_ma.mask] = np.maximum(output[output_ma.mask],0)
    return output

def detrend_pco2(ensemble_dir_head, ens, member, dates):
    member_dir = f"{ensemble_dir_head}/{ens}/member_{member}"
    xpco2_path = '/data/artemis/workspace/vbennington/NOAA_ESRL/atmos_pco2_3D_mon_198201-201701.nc'
    
    if ens == "CanESM2":
        # CanESM2 files are mislabeled as going to 201712
        pco2_path = f"{member_dir}/pCO2_2D_mon_{ens}{member}_1x1_198201-201712.nc"
    else:
        pco2_path = f"{member_dir}/pCO2_2D_mon_{ens}{member}_1x1_198201-201701.nc"
        
    pco2_subtract = xr.open_dataset(xpco2_path).pco2_subtract
    pco2_model = xr.open_dataset(pco2_path).pCO2
    ylat = pco2_model.ylat
    xlon = pco2_model.xlon
    time = pco2_model.time
    
    pco2_detrend = pco2_model
    
    pco2_detrend = pco2_model - np.array(pco2_subtract)
                 
    fname_out = f'/data/artemis/workspace/vbennington/detrend_atmos/LET/{ens}/detrended_pCO2_2D_mon_{ens}{member}_1x1_198201-201701.nc'
    ds3d_out = xr.Dataset(
        {
        'pco2_detrend':(['time','ylat','xlon'], pco2_detrend                       
        )},

        coords={
        'time': (['time'],pco2_model.time),
        'ylat': (['ylat'],pco2_model.ylat),
        'xlon': (['xlon'],pco2_model.xlon)
        })
        
    # Save to netcdf
    ds3d_out.to_netcdf(fname_out)

#===============================================
# Loading in data and creating features
#===============================================
def import_obs_data(N_time=456):
    
    sst_path = '/data/artemis/workspace/vbennington/NOAA_SST/processed/SST_mon_198201-202012.nc'                  # 1982-2020
    sss_path = '/data/artemis/workspace/vbennington/EN421_salinity/processed/sss_1x1_mon_EN421_198201-202011.nc'  # 1982-2020/11
    chl_path = '/data/artemis/workspace/vbennington/GlobColour/processed/CHL_interpFILL_mon_198201-202012.nc'     # 1982-2020
    #socat_path = '/data/artemis/workspace/vbennington/SOCAT/processed/dpCO2_WVCorr_monthly_197901-201912.nc'      # 1979-2019
    #socat_path = '/data/artemis/workspace/vbennington/SOCAT/processed/dpCO2_no_atm_seasonality_WVCorr_monthly_197901-201912.nc' # 1979-2019
    #socat_path = '/data/artemis/workspace/vbennington/SOCAT/processed/SOCATv2020_pCO2nonT_gridded_monthly.nc'     # 1982-2019, pCO2-nonT
    #socat_path = '/data/artemis/workspace/vbennington/SOCAT/processed/SOCAT_pCO2_pCO2T_pCO2DIC_2D_mon_1x1_198201-201912.nc'# 1982-2019, pCO2-DIC, pCO2-T, and pCO2
    #socat_path = '/data/artemis/workspace/vbennington/SOCAT/processed/SOCAT_PRODUCTS_pCO2_pCO2T_pCO2DIC_2D_mon_1x1_198201-201912.nc'# 1982-2019, pCO2-DIC, pCO2-T, and pCO2  (pCO2_mean from other products)
    socat_path = '/data/artemis/workspace/vbennington/SOCAT/processed/SOCAT_Takahashi_pCO2_pCO2T_pCO2DIC_2D_mon_1x1_198201-201912.nc' 
    mld_path = '/data/artemis/workspace/vbennington/mld_deboyer_1x1_clim_198201-201712.nc'                        # 12 month clim
    #atm_path = '/data/artemis/workspace/vbennington/Dickson_atmos/atmos_pCO2_WVCorr_mon_197901-201912.nc'         # 1979-2019
    atm_path = '/data/artemis/workspace/vbennington/Dickson_atmos/annual_atmos_pCO2_wvcorr_monthly_197901-201912.nc' # 1979-2019, no seasonal cycle of xCO2
    
    # Climate Indices #
    enso_path = '/data/artemis/observations/climate_indices/nino3.4_1979-2019.nc'
    nao_path = '/data/artemis/observations/climate_indices/NAO_index_1979-2019.nc'
    soi_path = '/data/artemis/observations/climate_indices/SOI_index_1979-2019.nc'
    sam_path = '/data/artemis/observations/climate_indices/SAM_index_1979-2019.nc'
    
    inputs = {}
    
    inputs['sst'] = xr.open_dataset(sst_path).sst[0:N_time,:,:]
    inputs['chl'] = xr.open_dataset(chl_path).chl[0:N_time,:,:]
    #inputs['dpco2'] = xr.open_dataset(socat_path).dpco2[36:36+N_time,:,:]
    #inputs['pCO2_nonT'] = xr.open_dataset(socat_path).pCO2_nonT
    inputs['pCO2_DIC'] =xr.open_dataset(socat_path).pCO2_DIC
    inputs['pCO2_T'] =xr.open_dataset(socat_path).pCO2_T
    inputs['pCO2'] =xr.open_dataset(socat_path).pCO2
    inputs['sss'] = xr.open_dataset(sss_path).sss[0:N_time,:,:]
    
    mld=np.tile(xr.open_dataset(mld_path).mld,(38,1,1))
    inputs['mld'] = xr.DataArray(data=mld,name='mld',dims=['time','ylat','xlon'])
    
    # Climate Indices (one dimensional and atm pco2) #
    DS_enso = xr.open_dataset(enso_path).enso_smooth_5mo[36:36+N_time] # Smoothed ENSO
    DS_nao = xr.open_dataset(nao_path).nao_smooth_5mo[36:36+N_time] # Smoothed NAO
    DS_soi = xr.open_dataset(soi_path).soi_smooth_5mo[36:36+N_time] # Smoothed SOI
    DS_sam = xr.open_dataset(sam_path).sam_smooth_5mo[36:36+N_time] # Smoothed SAM
    DS_xco2 = xr.open_dataset(atm_path).atmos_pco2[36:36+N_time] # 
    
    time = xr.open_dataset(sst_path).time[0:N_time]
    
    inputs['socat_mask'] = xr.open_dataset(socat_path).socat_mask  # this pCO2_nonT dataset socat_mask is 1982-2019
    
    for i in inputs:
        inputs[i] = inputs[i].transpose('time', 'ylat', 'xlon')
        time_len = len(time)
        inputs[i].assign_coords(time=time[0:time_len])
    
    
    DS = xr.merge([inputs['sss'], inputs['sst'], inputs['chl'], inputs['pCO2_DIC'], inputs['pCO2'], inputs['pCO2_T'], inputs['socat_mask'], inputs['mld']], compat='override', join='override')
    
    return DS, DS_xco2, DS_enso, DS_nao, DS_soi, DS_sam 

def import_obs_pco2_data(N_time=456):
    
    sst_path = '/data/artemis/workspace/vbennington/NOAA_SST/processed/SST_mon_198201-202012.nc'                  # 1982-2020
    sss_path = '/data/artemis/workspace/vbennington/EN421_salinity/processed/sss_1x1_mon_EN421_198201-202011.nc'  # 1982-2020/11
    chl_path = '/data/artemis/workspace/vbennington/GlobColour/processed/CHL_interpFILL_mon_198201-202012.nc'     # 1982-2020
    socat_path = '/data/artemis/workspace/vbennington/SOCAT/processed/SOCATv2020_pCO2_gridded_monthly.nc'         # 1979-2019
    mld_path = '/data/artemis/workspace/vbennington/mld_deboyer_1x1_clim_198201-201712.nc'                        # 12 month clim
    atm_path = '/data/artemis/workspace/vbennington/Dickson_atmos/atmos_pCO2_WVCorr_mon_197901-201912.nc'         # 1979-2019
    
    inputs = {}
    
    inputs['sst'] = xr.open_dataset(sst_path).sst[0:N_time,:,:]
    inputs['chl'] = xr.open_dataset(chl_path).chl[0:N_time,:,:]
    inputs['pco2'] = xr.open_dataset(socat_path).pco2_weighted[36:36+N_time,:,:]
    inputs['sss'] = xr.open_dataset(sss_path).sss[0:N_time,:,:]
    inputs['atm_pco2'] = xr.open_dataset(atm_path).pco2_atmos_3d[36:36+N_time,:,:]
    mld=np.tile(xr.open_dataset(mld_path).mld,(38,1,1))
    inputs['mld'] = xr.DataArray(data=mld,name='mld',dims=['time','ylat','xlon'])
    
    time = xr.open_dataset(sst_path).time[0:N_time]
    
    inputs['socat_mask'] = xr.open_dataset('/data/artemis/workspace/vbennington/SOCAT/processed/dpCO2_WVCorr_monthly_197901-201912.nc').socat_mask[36:36+N_time,:,:]
    
    for i in inputs:
        inputs[i] = inputs[i].transpose('time', 'ylat', 'xlon')
        time_len = len(time)
        inputs[i].assign_coords(time=time[0:time_len])
    
    
    DS = xr.merge([inputs['sss'], inputs['sst'], inputs['chl'], inputs['pco2'], inputs['socat_mask'], inputs['atm_pco2'], inputs['mld']], compat='override', join='override')
    print(DS)
    return DS


def create_obs_features(df, N_time=456, N_batch = 12):

    df['mld_log'] = log_or_0(df['mld'].values)
    #df['mld_anom'] = calc_anom(df['mld_log'].values, N_time, N_batch) - no anomalies need because climatology used
    
    df['chl_log'] = log_or_0(df['chl'].values)
 
    df['chl_anom'] = calc_interannual_anom(df['chl_log'])
    df['sss_anom'] = calc_interannual_anom(df['sss'])
    df['sst_anom'] = calc_interannual_anom(df['sst'])
        
    days_idx = df.index.get_level_values('time').dayofyear
    lon_rad = np.radians(df.index.get_level_values('xlon').to_numpy())
    lat_rad = np.radians(df.index.get_level_values('ylat').to_numpy())
    df['T0'], df['T1'] = [np.cos(days_idx * 2 * np.pi / 365), np.sin(days_idx * 2 * np.pi / 365)]
    df['A'], df['B'], df['C'] = [np.sin(lat_rad), np.sin(lon_rad)*np.cos(lat_rad), -np.cos(lon_rad)*np.cos(lat_rad)]
    #df['cosLON'], df['sinLON'] = [np.cos(lon_rad), np.sin(lon_rad)]

    return df


def create_inputs(dates, N_batch = 12):
    
    DS, DS_xco2, DS_enso, DS_nao, DS_soi, DS_sam  = import_obs_data(N_time=len(dates))
    
    df = DS.to_dataframe()
    #print(df)
    df = create_obs_features(df, N_time=len(dates), N_batch=N_batch) 
    
    net_mask = np.tile(network_mask().transpose('lon','lat').to_dataframe()['mask'].to_numpy(), len(dates))
    
    df['net_mask'] = net_mask
    df['enso'] = np.repeat(DS_enso.values, 360*180)
    df['nao'] = np.repeat(DS_nao.values, 360*180)
    df['soi'] = np.repeat(DS_soi.values, 360*180)
    df['xco2'] = np.repeat(DS_xco2.values, 360*180)
    df['sam'] = np.repeat(DS_sam.values, 360*180)
    
    return df


def create_pco2_inputs(dates, N_batch=12):

    DS = import_obs_pco2_data(N_time=len(dates))
    
    df = DS.to_dataframe()
    df = create_obs_features(df, N_time=len(dates), N_batch=N_batch) 
    
    net_mask = np.tile(network_mask().transpose('lon','lat').to_dataframe()['mask'].to_numpy(), len(dates))
    df['net_mask'] = net_mask

    return df

#===============================================
# Evaluation functions
#===============================================

def centered_rmse(y,pred):
    y_mean = np.mean(y)
    pred_mean = np.mean(pred)
    return np.sqrt(np.square((pred - pred_mean) - (y - y_mean)).sum()/pred.size)

def evaluate_test(y, pred):
    scores = {
        'mse':mean_squared_error(y, pred),
        'mae':mean_absolute_error(y, pred),
        'medae':median_absolute_error(y, pred),
        'max_error':max_error(y, pred),
        'bias':pred.mean() - y.mean(),
        'r2':r2_score(y, pred),
        'corr':np.corrcoef(y,pred)[0,1],
        'cent_rmse':centered_rmse(y,pred),
        'stdev' :np.std(pred),
        'amp_ratio':(np.max(pred)-np.min(pred))/(np.max(y)-np.min(y)), # added when doing temporal decomposition
        'stdev_ref':np.std(y),
        'range_ref':np.max(y)-np.min(y),
        'iqr_ref':np.subtract(*np.percentile(y, [75, 25]))
        }
    return scores

#===============================================
# Train test split functions
#===============================================
def train_val_test_split(N, test_prop, val_prop, random_seeds, ens_count):
    intermediate_idx, test_idx = train_test_split(range(N), test_size=test_prop, random_state=random_seeds[0,ens_count])
    train_idx, val_idx = train_test_split(intermediate_idx, test_size=val_prop/(1-test_prop), random_state=random_seeds[1,ens_count])
    return intermediate_idx, train_idx, val_idx, test_idx

def apply_splits(X, y, train_val_idx, train_idx, val_idx, test_idx):
    X_train_val = X[train_val_idx,:]
    X_train = X[train_idx,:]
    X_val = X[val_idx,:]
    X_test = X[test_idx,:]

    y_train_val = y[train_val_idx]
    y_train = y[train_idx]
    y_val = y[val_idx]
    y_test = y[test_idx]

    return X_train_val, X_train, X_val, X_test, y_train_val, y_train, y_val, y_test

def cross_val_splits(train_val_idx, random_seeds, row, ens_count, folds=3):
    """Didn't actually use this"""
    idx = train_val_idx.copy()
    np.random.seed(random_seeds[row,ens_count])
    np.random.shuffle(idx)
    list_val = np.array_split(idx, folds)
    list_train = []
    for i in range(folds):
        list_train.append( np.concatenate(list_val[:i] + list_val[(i+1):]) )
    return zip(list_train, list_val)

#===============================================
# NN functions
#===============================================

def build_nn(num_features, neurons=[512,256], act='relu', use_drop=True, drop_rate=0.5, learning_rate=0.01, reg=0.001):
    model = Sequential()
    model.add(BatchNormalization(input_shape=(num_features,)))
    for i in range(len(neurons)):
        model.add(Dense(units=neurons[i], activation=act, kernel_regularizer=regularizers.l2(reg)))
        if use_drop:
            model.add(Dropout(drop_rate))
    model.add(Dense(units=1))

    model.compile(keras.optimizers.Adam(lr=learning_rate), loss='mse', metrics=['mse'])

    return model

def build_nn_vf(num_features, act='relu', learning_rate=0.01, reg=0.001):
    model = Sequential()
    model.add(BatchNormalization(input_shape=(num_features,)))
    model.add(Dense(units=500, activation=act, kernel_regularizer=regularizers.l2(reg)))
    model.add(Dense(units=500, activation=act, kernel_regularizer=regularizers.l2(reg)))
    model.add(Dense(units=1))

    model.compile(keras.optimizers.Adam(lr=learning_rate), loss='mse', metrics=['mse'])

    return model


#===============================================
# Saving functions
#===============================================

def save_clean_data(df, data_output_dir):
    print("Starting data saving process")
    output_dir = f"{data_output_dir}"
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    fname = f"{output_dir}/data_clean_2D_mon_1x1_198201-201912.pkl"
    df.to_pickle(fname)
    print("Save complete")

def save_model(model, output_dir, approach, run=None):
    print("Starting model saving process")
    out_dir = f"{output_dir}/{approach}"
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    if approach == 'nn':
        if run is None:
            run = 0
        fname = f"{out_dir}/{approach}_dpCO2_2D_mon_{run}_1x1_198201-201912.h5"
        model.save(fname)
    else:
        fname = f"{out_dir}/{approach}_dpCO2_2D_mon_1x1_198201-201912.joblib"
        joblib.dump(model, fname)
    print("Save complete")

def save_recon(DS_recon, recon_output_dir, approach, run=None):
    print("Starting reconstruction saving process")
    recon_dir = f"{recon_output_dir}/{approach}"
    Path(recon_dir).mkdir(parents=True, exist_ok=True)
    if approach == "nn":
        if run is None:
            run = 0
        recon_fname = f"{recon_dir}/{approach}_recon_dpCO2_2D_mon_{run}_1x1_198201-201912.nc"
    else:
        recon_fname = f"{recon_dir}/{approach}_recon_dpCO2_2D_mon_1x1_198201-201912.nc"
    DS_recon.to_netcdf(recon_fname)
    print("Save complete")


#===============================================
# Temporal deconstruction functions
#===============================================

def detrend_time_2d(array_1d, N_time):
    """
        Input: 1d array and the length the time dimension should be
        Output: 2d array of original data less linear trend over time
        Method: assumes 2d array can be filled in column-wise (i.e. time was the first dimension that generated the 1d array)
    """
    array_2d = array_1d.reshape(N_time,-1,order='C')
    nan_mask = (np.all(np.isnan(array_2d), axis=0))
    X = np.arange( N_time )
    try:
        regressions = np.polyfit(X, array_2d[:,~nan_mask], 1)
    except:
        regressions = np.empty((2,np.sum(~nan_mask)))
        j = 0
        for i in range(nan_mask.shape[0]):
            if ~nan_mask[i]:
                regress = np.polyfit(X[~np.isnan(array_2d[:,i])], array_2d[~np.isnan(array_2d[:,i]),i], 1)
                regressions[:,j] = regress
                j +=1
    lin_fit = (np.expand_dims(X,1) * regressions[0:1,:] + regressions[1:,:])
    array_detrend_2d = np.ones(shape=array_2d.shape)*np.nan
    array_detrend_2d[:,~nan_mask] = array_2d[:,~nan_mask] - lin_fit
    
    return array_detrend_2d

def calc_seasonal(array_2d, N_time=421, period=12):
    nan_mask = ~(np.all(np.isnan(array_2d), axis=0))
    month_avgs = np.array([np.nanmean(array_2d[i::period,nan_mask], axis=0) for i in range(period)])
    month_avgs_centered = month_avgs - np.nanmean(month_avgs, axis=0)
    
    seasonal = np.ones(array_2d.shape)*np.NaN
    seasonal[:,nan_mask] = np.tile(month_avgs_centered, [N_time // period + 1,1])[:N_time]
    
    return seasonal
                     

def apply_lowess(array_2d, x, frac_lo, it_lo, delta_lo):
    nan_mask = ~(np.all(np.isnan(array_2d), axis=0))
    smoothed = np.apply_along_axis(lowess, 0, array_2d[:,nan_mask],
                               exog=x, frac=frac_lo, it=it_lo, delta=delta_lo, 
                               is_sorted=True, missing="drop", return_sorted=False)
    out = np.ones(array_2d.shape)*np.NaN
    out[:,nan_mask] = smoothed
    
    return out


def detrend(y, N_time, period, x, frac_lo, frac_resid_lo, it_lo, delta_lo):
    y_detrend = detrend_time_2d(y, N_time)
    y_seasonal = calc_seasonal(y_detrend, N_time, period)
    y_deseason = y_detrend - y_seasonal
    y_decadal = apply_lowess(y_deseason, x, frac_lo, it_lo, delta_lo)
    y_resid = y_deseason - y_decadal
    y_resid_lo = apply_lowess(y_resid, x, frac_resid_lo, it_lo, delta_lo)
    
    return y_detrend, y_seasonal, y_decadal, y_resid, y_resid_lo

def apply_detrend(approach, ens, member, recon_output_dir, nn_val_df):

    N_time = 421
    period = 12
    x = np.arange(N_time)
    it_lo = 3
    delta_lo = 0.01 * N_time
    frac_lo = 12*10 / N_time
    frac_resid_lo = 12 / N_time

    #recon_output_dir = "/local/data/artemis/workspace/jfs2167/recon_eval/models/reconstructions"
    recon_dir = f"{recon_output_dir}/{approach}/{ens}/member_{member}"
            
    if approach == "nn":
        run = nn_val_df.query("model == @ens and member == @member and sel_min_bias_mse == 1").index.values[0][2]
        recon_fname = f"{recon_dir}/{approach}_recon_pC02_2D_mon_{ens}_{member}_{run}_1x1_198201-201701.nc"
        recon_fname_out = f"{recon_dir}/{approach}_recon_temporal_pC02_2D_mon_{ens}_{member}_{run}_1x1_198201-201701.nc"
    else:
        recon_fname = f"{recon_dir}/{approach}_recon_pC02_2D_mon_{ens}_{member}_1x1_198201-201701.nc"
        recon_fname_out = f"{recon_dir}/{approach}_recon_temporal_pC02_2D_mon_{ens}_{member}_1x1_198201-201701.nc"

    DS_recon = xr.load_dataset(recon_fname)
    df = DS_recon.to_dataframe()

    y = df['pCO2'].values
    r = df['pCO2_recon'].values

    col_sel = ["net_mask", "socat_mask"]
    data_types = ["detrend", "seasonal", "decadal", "resid", "resid_lo"]
    if approach == "rf": # Only deconstruct the raw data for one of the reconstructions so don't need to duplicate computation
        y_output = detrend(y, N_time, period, x, frac_lo, frac_resid_lo, it_lo, delta_lo)
        col_sel.append("pCO2")
        for i in range(len(data_types)):
            df[f"pCO2_{data_types[i]}"] = y_output[i].flatten(order='C')
            col_sel.append(f"pCO2_{data_types[i]}")
        
    r_output = detrend(r, N_time, period, x, frac_lo, frac_resid_lo, it_lo, delta_lo)
    col_sel.append("pCO2_recon")
    for i in range(len(data_types)):
        df[f"pCO2_recon_{data_types[i]}"] = r_output[i].flatten(order='C')
        col_sel.append(f"pCO2_recon_{data_types[i]}")

    DS_recon = df[col_sel].to_xarray()
    DS_recon.to_netcdf(recon_fname_out)

def apply_detrend_dpco2(approach, ens, member, recon_output_dir, nn_val_df):

    N_time = 421
    period = 12
    x = np.arange(N_time)
    it_lo = 3
    delta_lo = 0.01 * N_time
    frac_lo = 12*10 / N_time
    frac_resid_lo = 12 / N_time

    #recon_output_dir = "/local/data/artemis/workspace/jfs2167/recon_eval/models/reconstructions"
    recon_dir = f"{recon_output_dir}/{approach}/{ens}/member_{member}"
            
    if approach == "nn":
        run = nn_val_df.query("model == @ens and member == @member and sel_min_bias_mse == 1").index.values[0][2]
        recon_fname = f"{recon_dir}/{approach}_recon_pC02_2D_mon_{ens}_{member}_{run}_1x1_198201-201701.nc"
        recon_fname_out = f"{recon_dir}/{approach}_recon_temporal_pC02_2D_mon_{ens}_{member}_{run}_1x1_198201-201701.nc"
    else:
        recon_fname = f"{recon_dir}/{approach}_recon_pC02_2D_mon_{ens}_{member}_1x1_198201-201701.nc"
        recon_fname_out = f"{recon_dir}/{approach}_recon_temporal_pC02_2D_mon_{ens}_{member}_1x1_198201-201701.nc"

    DS_recon = xr.load_dataset(recon_fname)
    df = DS_recon.to_dataframe()

    y = df['delta_pCO2_wvcorr'].values
    r = df['delta_pCO2_recon'].values

    col_sel = ["net_mask", "socat_mask"]
    data_types = ["detrend", "seasonal", "decadal", "resid", "resid_lo"]
    if approach == "rf": # Only deconstruct the raw data for one of the reconstructions so don't need to duplicate computation
        y_output = detrend(y, N_time, period, x, frac_lo, frac_resid_lo, it_lo, delta_lo)
        col_sel.append("delta_pCO2_wvcorr")
        for i in range(len(data_types)):
            df[f"delta_pCO2_wvcorr_{data_types[i]}"] = y_output[i].flatten(order='C')
            col_sel.append(f"delta_pCO2_wvcorr_{data_types[i]}")
        
    r_output = detrend(r, N_time, period, x, frac_lo, frac_resid_lo, it_lo, delta_lo)
    col_sel.append("delta_pCO2_recon")
    for i in range(len(data_types)):
        df[f"delta_pCO2_recon_{data_types[i]}"] = r_output[i].flatten(order='C')
        col_sel.append(f"delta_pCO2_recon_{data_types[i]}")

    DS_recon = df[col_sel].to_xarray()
    DS_recon.to_netcdf(recon_fname_out)