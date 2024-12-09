from rasterio.transform import from_origin
from datetime import datetime as dt
from scipy.spatial import cKDTree
from dotenv import load_dotenv
import geopandas as gpd
import netCDF4 as nc
import pandas as pd
import numpy as np
import rasterio
import requests
import datetime
import ftplib
import cftime
import os

load_dotenv()
os.environ["PROJ_LIB"] = "C:/Users/2ndba/anaconda3/Library/share/proj"

geoserver_endpoint = os.getenv("GEOSERVER_ENDPOINT")
workspace = os.getenv("WORKSPACE")

output_extracted_point_island_folder = "repository/output/daily/result/pulau"
output_extracted_point_balai_folder = "repository/output/daily/result/balai"
boundry_island_data = "repository/input/data_vektor/sampel_pch_pulaui.shp"
boundry_balai_data = "repository/input/data_vektor/sampel_pch_balai.shp"
download_precipitation_path_raster = "repository/input/data_raster"
output_csv_to_idw = "repository/output/daily/csv_to_idw"
output_nc_to_csv = "repository/output/daily/nc_to_csv"

# konfigurasi ftp
ftp_host = os.getenv("HOST")
ftp_user = os.getenv("USER")
ftp_password = os.getenv("PASSWORD")
cycle = "12"
os.makedirs(download_precipitation_path_raster, exist_ok=True)

# FTP functions
def connect_ftp():
    ftp = ftplib.FTP(ftp_host)
    ftp.login(ftp_user, ftp_password)
    ftp.cwd("/")
    return ftp

def download_file_from_ftp(ftp, filename):
    file_list = ftp.nlst()
    if filename in file_list:
        local_file_path = os.path.join(download_precipitation_path_raster, filename)
        if not os.path.exists(local_file_path):
            with open(local_file_path, "wb") as local_file:
                ftp.retrbinary(f"RETR {filename}", local_file.write)
            print(f"Berhasil download file {filename}")
        else:
            print(f"File {filename} sudah tersedia")
        return local_file_path
    return None

def download_latest_file_from_ftp(ftp):
    file_list = ftp.nlst()
    if file_list:
        latest_file = sorted(file_list)[-1]
        return download_file_from_ftp(ftp, latest_file)
    return None

# Download file .nc
today = datetime.date.today() - datetime.timedelta(days=1)
filename = f"ECMWF.0125.{today.strftime('%Y%m%d')}{cycle}00.PREC.nc"
print("Sedang mengunduh:", filename)

ftp = connect_ftp()
if ftp:
    local_file_path = download_file_from_ftp(ftp, filename) or download_latest_file_from_ftp(ftp)
    ftp.quit()
else:
    print("Tidak dapat terhubung ke server FTP.")

if local_file_path is None:
    print("File tidak tersedia untuk didownload")
    exit()

# Function to convert time from NetCDF
def convert_time(times, time_units, time_calendar):
    time_converted = nc.num2date(times, units=time_units, calendar=time_calendar)
    time_converted_utc = []
    for time_value in time_converted:
        if isinstance(time_value, cftime.datetime):
            time_value = dt(
                time_value.year, time_value.month, time_value.day,
                time_value.hour, time_value.minute, time_value.second
            )
        time_converted_utc.append(time_value)
    return pd.to_datetime(time_converted_utc).tz_localize('UTC')

# Process NetCDF and aggregate by 3 hours
def process_netcdf(local_file_path):
    dataset = nc.Dataset(local_file_path)

    latitudes = dataset.variables['lat'][:]
    longitudes = dataset.variables['lon'][:]
    times = dataset.variables['time'][:]
    rainfall = dataset.variables['tp'][:]

    time_units = dataset.variables['time'].units
    time_calendar = dataset.variables['time'].calendar if hasattr(dataset.variables['time'], 'calendar') else 'standard'
    
    time_converted_utc = convert_time(times, time_units, time_calendar)

    time_converted_3hr = time_converted_utc.round('3H')

    data = []
    for time_index, time_value in enumerate(time_converted_3hr):
        for lat_index, lat in enumerate(latitudes):
            for lon_index, lon in enumerate(longitudes):
                rain_value = rainfall[time_index, 0, lat_index, lon_index]
                data.append([round(lat, 15), round(lon, 15), time_value, rain_value])

    df = pd.DataFrame(data, columns=['y', 'x', 'time', 'z'])

    df['Year'] = df['time'].dt.year
    df['Month'] = df['time'].dt.month
    df['Day'] = df['time'].dt.day
    df['Hour'] = df['time'].dt.hour

#akumulasi harian
    df_daily = df.groupby(['Year', 'Month', 'Day', 'x', 'y'], as_index=False).agg({
            'time': 'first',  # Simpan waktu pertama dalam grup (opsional)
            'z': 'sum'        # Jumlahkan curah hujan untuk akumulasi harian
        })

    return df_daily

# Save processed data to CSV
def save_to_csv(df_daily, output_nc_to_csv):
    unique_times = df_daily['time'].unique()

    for unique_time in unique_times:
        filtered_df = df_daily[df_daily['time'] == unique_time].copy()
        filtered_df['time'] = filtered_df['time'].dt.tz_localize(None)

        # Save as CSV
        time_str = unique_time.strftime('%m%d%Y_%H%M')
        output_file = f"{output_nc_to_csv}/pch_day_{time_str}.csv"
        
        filtered_df[['y', 'x', 'time', 'z']].to_csv(output_file, index=False)
        print(f"Data berhasil disimpan di {output_file}")
        
    return unique_times

# IDW interpolation function
def idw_interpolation(x, y, z, xi, yi, power=2):
    tree = cKDTree(np.array(list(zip(x, y))))
    dist, idx = tree.query(np.array(list(zip(xi.ravel(), yi.ravel()))), k=10)
    weights = 1 / dist**power
    weights /= weights.sum(axis=1, keepdims=True)
    zi = np.sum(z[idx] * weights, axis=1)
    return zi.reshape(xi.shape)

# Upload data to GeoServer
def upload_to_geoserver(data_path, store_name):
    file_extension = os.path.splitext(data_path)[1].lower()
    if file_extension == ".shp":
        file_type = "shp"
        store_type = "datastores"
    elif file_extension == ".tif":
        file_type = "geotiff"
        store_type = "coveragestores"
    else:
        print("Tipe file tidak didukung")
        return None
    
    absolute_path = os.path.abspath(data_path).replace("\\", "/")
    url = f"{geoserver_endpoint}/rest/workspaces/{workspace}/{store_type}/{store_name}/external.{file_type}"
    print("url data geotiff:", url)

    headers = {"Content-type": "text/plain"}
    response = requests.put(url, data=f"file://{absolute_path}", headers=headers)

    if response.status_code in [200, 201]:
        print(f"Berhasil upload {data_path} ke geoserver.")
        return True
    else:
        print(f"Gagal upload {data_path} ke geoserver. Status code: {response.status_code}")
        return False

# Interpolate and save to GeoTIFF
def interpolate_and_save_to_tiff(df_daily, output_csv_to_idw, geoserver_endpoint, workspace):
    unique_times = df_daily['time'].unique()

    for unique_time in unique_times:
        filtered_df = df_daily[df_daily['time'] == unique_time].copy()
        x = filtered_df['x'].values
        y = filtered_df['y'].values
        z = filtered_df['z'].values

        # Define grid for interpolation
        xmin, xmax = x.min(), x.max()
        ymin, ymax = y.min(), y.max()
        res = 0.092  # Grid resolution in degrees
        grid_x, grid_y = np.meshgrid(np.arange(xmin, xmax + res, res), np.arange(ymin, ymax + res, res))

        # IDW Interpolation
        grid_z = idw_interpolation(x, y, z, grid_x, grid_y)

        # Save to GeoTIFF
        time_str = unique_time.strftime('%m%d%Y_%H%M')
        output_tiff = f"{output_csv_to_idw}/pch_day_{time_str}.tif"
        transformasi = from_origin(xmin, ymax, res, res)

        crs = rasterio.crs.CRS.from_proj4("+proj=longlat +datum=WGS84 +no_defs")
        with rasterio.open(output_tiff, 'w', driver='GTiff', height=grid_z.shape[0], width=grid_z.shape[1],
                           count=1, dtype=grid_z.dtype, crs=crs, transform=transformasi) as dst:
            dst.write(grid_z, 1)

        print(f"Hasil interpolasi disimpan di {output_tiff}")

        # Upload to GeoServer
        store_name = os.path.splitext(os.path.basename(output_tiff))[0]
        print("store name", store_name)
        if upload_to_geoserver(output_tiff, store_name):
            print(f"File TIFF berhasil diunggah ke GeoServer: {output_tiff}")
        else:
            print(f"Gagal mengunggah file TIFF: {output_tiff}")

def process_extraction(boundary_data, raster_file, output_folder, prefix):
    # Baca shapefile
    extract_point = gpd.read_file(boundary_data)
    print(f"Memproses file: {boundary_data} dengan raster: {raster_file}")

    # Buka file raster
    with rasterio.open(raster_file) as src:
        raster_data = src.read(1)
        raster_transform = src.transform

    # Ekstraksi nilai raster berdasarkan titik
    extracted_values = []
    for _, row in extract_point.iterrows():
        x, y = row.geometry.x, row.geometry.y
        col_idx, row_idx = ~raster_transform * (x, y)
        col_idx, row_idx = int(col_idx), int(row_idx)
        value = raster_data[row_idx, col_idx]
        extracted_values.append(value)

    extract_point['value'] = extracted_values

    # Klasifikasi curah hujan
    def classify_grid_kl(val):
        if 0.00 <= val < 1.12:
            return 1
        elif 1.12 <= val < 2.81:
            return 2
        elif 2.81 <= val < 5.62:
            return 3
        elif 5.62 <= val < 8.43:
            return 4
        elif 8.43 <= val:
            return 5
        else:
            return 1

    # Klasifikasi kesiapsiagaan bencana
    def classify_grid_kg(val):
        if 0.00 <= val < 2.81:
            return 1
        elif 2.81 <= val < 4.21:
            return 2
        elif 4.21 <= val < 5.62:
            return 3
        elif 5.62 <= val:
            return 4
        else:
            return 1

    # Tambahkan kolom klasifikasi dan kolom kesiapsiagaan
    extract_point['grid_kl'] = extract_point['value'].apply(classify_grid_kl)
    extract_point['grid_kg'] = extract_point['value'].apply(classify_grid_kg)

    # Tambahkan kolom kelas
    kelas_columns = ['kelas_kl_1', 'kelas_kl_2', 'kelas_kl_3', 'kelas_kl_4', 'kelas_kl_5']
    for col in kelas_columns:
        extract_point[col] = 0

    # Perhitungan `kelas_1` hingga `kelas_5` berdasarkan `kode_kk`
    grouped = extract_point.groupby('kode_kk')

    for kode_pulau, group in grouped:
        # Hitung jumlah kemunculan setiap nilai grid_kl dalam grup
        grid_kl_counts = group['grid_kl'].value_counts().to_dict()

        # Update nilai kelas berdasarkan grid_kl
        extract_point.loc[extract_point['kode_kk'] == kode_pulau, 'kelas_kl_1'] = grid_kl_counts.get(1, 0)
        extract_point.loc[extract_point['kode_kk'] == kode_pulau, 'kelas_kl_2'] = grid_kl_counts.get(2, 0)
        extract_point.loc[extract_point['kode_kk'] == kode_pulau, 'kelas_kl_3'] = grid_kl_counts.get(3, 0)
        extract_point.loc[extract_point['kode_kk'] == kode_pulau, 'kelas_kl_4'] = grid_kl_counts.get(4, 0)
        extract_point.loc[extract_point['kode_kk'] == kode_pulau, 'kelas_kl_5'] = grid_kl_counts.get(5, 0)

    # Hitung total untuk setiap kategori di grid_kl
    grid_kl_counts_total = extract_point['grid_kl'].value_counts().to_dict()
    extract_point['total_kl_1'] = grid_kl_counts_total.get(1, 0)
    extract_point['total_kl_2'] = grid_kl_counts_total.get(2, 0)
    extract_point['total_kl_3'] = grid_kl_counts_total.get(3, 0)
    extract_point['total_kl_4'] = grid_kl_counts_total.get(4, 0)
    extract_point['total_kl_5'] = grid_kl_counts_total.get(5, 0)

    # tambahkan kolom kelas kesiapsiagaan
    kelas_columns_kesiapsiagaan = ['kelas_kg_1', 'kelas_kg_2', 'kelas_kg_3', 'kelas_kg_4']
    for col_kesiapsiagaan in kelas_columns_kesiapsiagaan:
        extract_point[col_kesiapsiagaan] = 0

    # perhitungan kelas_kg_1 sampai kelas_kg_4
    grouped_kesiapsiagaan = extract_point.groupby('kode_kk')

    for kode_pulau, group_kesiapsiagaan in grouped_kesiapsiagaan:
        # Hitung jumlah kemunculan setiap nilai grid_kl dalam grup
        grid_kg_counts = group_kesiapsiagaan['grid_kg'].value_counts().to_dict()

        # Update nilai kelas berdasarkan grid_kl
        extract_point.loc[extract_point['kode_kk'] == kode_pulau, 'kelas_kg_1'] = grid_kg_counts.get(1, 0)
        extract_point.loc[extract_point['kode_kk'] == kode_pulau, 'kelas_kg_2'] = grid_kg_counts.get(2, 0)
        extract_point.loc[extract_point['kode_kk'] == kode_pulau, 'kelas_kg_3'] = grid_kg_counts.get(3, 0)
        extract_point.loc[extract_point['kode_kk'] == kode_pulau, 'kelas_kg_4'] = grid_kg_counts.get(4, 0)

    # Hitung total untuk setiap kategori di grid_kg
    grid_kg_counts_total = extract_point['grid_kg'].value_counts().to_dict()
    extract_point['total_kg_1'] = grid_kg_counts_total.get(1, 0)
    extract_point['total_kg_2'] = grid_kg_counts_total.get(2, 0)
    extract_point['total_kg_3'] = grid_kg_counts_total.get(3, 0)
    extract_point['total_kg_4'] = grid_kg_counts_total.get(4, 0)

    # Hapus duplikasi dan hanya ambil satu baris per `kode_kk` berdasarkan nilai tertinggi pada kolom `value`
    extract_point = extract_point.loc[extract_point.groupby('kode_kk')['value'].idxmax()]

    # Simpan ke shapefile
    output_file = os.path.join(output_folder, f"{prefix}_{os.path.basename(raster_file).replace('.tif', '.shp')}")
    extract_point.to_file(output_file, driver="ESRI Shapefile")
    print(f"Berhasil ekstrak data raster {output_file}")

    # Ambil nama file tanpa ekstensi .shp untuk digunakan sebagai store
    store_name = os.path.splitext(os.path.basename(output_file))[0]
    # print("store name", store_name)

    # Upload shapefile ke GeoServer
    if upload_to_geoserver(output_file, store_name):
        print(f"File TIFF berhasil diunggah ke GeoServer: {output_file}")
    else:
        print(f"Gagal mengunggah file TIFF: {output_file}")

# Looping melalui semua file TIFF di folder
for tif_file in os.listdir(output_csv_to_idw):
    if tif_file.endswith('.tif'):
        raster_path = os.path.join(output_csv_to_idw, tif_file)

        # Proses data pulau
        process_extraction(boundry_island_data, raster_path, output_extracted_point_island_folder, "pulau")
        
        # Proses data balai
        process_extraction(boundry_balai_data, raster_path, output_extracted_point_balai_folder, "balai")

def process_netcdf_and_interpolate_with_extraction(
    local_file_path, 
    output_nc_to_csv, 
    output_csv_to_idw, 
    geoserver_endpoint, 
    workspace, 
    boundry_island_data, 
    boundry_balai_data, 
    output_extracted_point_island_folder, 
    output_extracted_point_balai_folder
):
    # Langkah 1: Proses NetCDF dan Simpan CSV
    df_daily = process_netcdf(local_file_path)
    save_to_csv(df_daily, output_nc_to_csv)
    
    # Langkah 2: Interpolasi dan Simpan GeoTIFF
    interpolate_and_save_to_tiff(df_daily, output_csv_to_idw, geoserver_endpoint, workspace)
    
    # Langkah 3: Ekstraksi Data Raster untuk Setiap File GeoTIFF
    for tif_file in os.listdir(output_csv_to_idw):
        if tif_file.endswith('.tif'):
            raster_path = os.path.join(output_csv_to_idw, tif_file)
            
            # Proses data pulau
            process_extraction(boundry_island_data, raster_path, output_extracted_point_island_folder, "pulau")
            
            # Proses data balai
            process_extraction(boundry_balai_data, raster_path, output_extracted_point_balai_folder, "balai")

process_netcdf_and_interpolate_with_extraction(
    local_file_path, 
    output_nc_to_csv, 
    output_csv_to_idw, 
    geoserver_endpoint, 
    workspace, 
    boundry_island_data, 
    boundry_balai_data, 
    output_extracted_point_island_folder, 
    output_extracted_point_balai_folder
)
