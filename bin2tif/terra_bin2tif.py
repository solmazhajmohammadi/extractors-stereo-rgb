#!/usr/bin/env python

"""
This extractor triggers when a file is added to a dataset in Clowder.

It checks for _left and _right BIN files to convert them into
JPG and TIF formats.
 """

import os
import logging
import shutil
import datetime

from pyclowder.extractors import Extractor
from pyclowder.utils import CheckMessage
import pyclowder.files
import pyclowder.datasets
import terrautils.extractors

import bin_to_geotiff as bin2tiff


class StereoBin2JpgTiff(Extractor):
    def __init__(self):
        Extractor.__init__(self)

        influx_host = os.getenv("INFLUXDB_HOST", "terra-logging.ncsa.illinois.edu")
        influx_port = os.getenv("INFLUXDB_PORT", 8086)
        influx_db = os.getenv("INFLUXDB_DB", "extractor_db")
        influx_user = os.getenv("INFLUXDB_USER", "terra")
        influx_pass = os.getenv("INFLUXDB_PASSWORD", "")

        # add any additional arguments to parser
        self.parser.add_argument('--output', '-o', dest="output_dir", type=str, nargs='?',
                                 default="/home/extractor/sites/ua-mac/Level_1/stereoTop_geotiff",
                                 help="root directory where timestamp & output directories will be created")
        self.parser.add_argument('--overwrite', dest="force_overwrite", type=bool, nargs='?', default=False,
                                 help="whether to overwrite output file if it already exists in output directory")
        self.parser.add_argument('--influxHost', dest="influx_host", type=str, nargs='?',
                                 default="terra-logging.ncsa.illinois.edu", help="InfluxDB URL for logging")
        self.parser.add_argument('--influxPort', dest="influx_port", type=int, nargs='?',
                                 default=8086, help="InfluxDB port")
        self.parser.add_argument('--influxUser', dest="influx_user", type=str, nargs='?',
                                 default="terra", help="InfluxDB username")
        self.parser.add_argument('--influxPass', dest="influx_pass", type=str, nargs='?',
                                 default=influx_pass, help="InfluxDB password")
        self.parser.add_argument('--influxDB', dest="influx_db", type=str, nargs='?',
                                 default="extractor_db", help="InfluxDB database")

        # parse command line and load default logging configuration
        self.setup()

        # setup logging for the exctractor
        logging.getLogger('pyclowder').setLevel(logging.DEBUG)
        logging.getLogger('__main__').setLevel(logging.DEBUG)

        # assign other arguments
        self.output_dir = self.args.output_dir
        self.force_overwrite = self.args.force_overwrite
        self.influx_params = {
            "host": influx_host,
            "port": influx_port,
            "db": influx_db,
            "user": influx_user,
            "pass": influx_pass
        }

    def check_message(self, connector, host, secret_key, resource, parameters):
        if not terrautils.extractors.is_latest_file(resource):
            return CheckMessage.ignore

        # Check for a left and right BIN file - skip if not found
        found_left = False
        found_right = False
        for f in resource['files']:
            if 'filename' in f:
                if f['filename'].endswith('_left.bin'):
                    found_left = True
                elif f['filename'].endswith('_right.bin'):
                    found_right = True
        if not (found_left and found_right):
            return CheckMessage.ignore

        # Check if outputs already exist unless overwrite is forced - skip if found
        out_dir = terrautils.extractors.get_output_directory(self.output_dir, resource['dataset_info']['name'])
        if not self.force_overwrite:
            lbase = os.path.join(out_dir, terrautils.extractors.get_output_filename(
                    resource['dataset_info']['name'], '', opts=['left']))
            rbase = os.path.join(out_dir, terrautils.extractors.get_output_filename(
                    resource['dataset_info']['name'], '', opts=['right']))
            if (os.path.isfile(lbase+'jpg') and os.path.isfile(rbase+'jpg') and
                    os.path.isfile(lbase+'tif') and os.path.isfile(rbase+'tif')):
                logging.info("skipping dataset %s; outputs found in %s" % (resource['id'], out_dir))
                return CheckMessage.ignore

        # Check metadata to verify we have what we need
        md = pyclowder.datasets.download_metadata(connector, host, secret_key, resource['id'])
        found_meta = False
        for m in md:
            # If there is metadata from this extractor, assume it was previously processed
            if not self.force_overwrite:
                if 'agent' in m and 'name' in m['agent']:
                    if m['agent']['name'].endswith(self.extractor_info['name']):
                        logging.info("skipping dataset %s; metadata indicates it was already processed" % resource['id'])
                        return CheckMessage.ignore
            if 'content' in m and 'lemnatec_measurement_metadata' in m['content']:
                found_meta = True

        if found_left and found_right and found_meta:
            return CheckMessage.download
        else:
            return CheckMessage.ignore

    def process_message(self, connector, host, secret_key, resource, parameters):
        starttime = datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
        created = 0
        bytes = 0

        img_left = None
        img_right = None
        metadata = None

        # Determine output location & filenames
        out_dir = terrautils.extractors.get_output_directory(self.output_dir, resource['dataset_info']['name'])
        logging.info("...output directory: %s" % out_dir)
        if not os.path.exists(out_dir):
            os.makedirs(out_dir)
        lbase = os.path.join(out_dir, terrautils.extractors.get_output_filename(
                resource['dataset_info']['name'], '', opts=['left']))
        rbase = os.path.join(out_dir, terrautils.extractors.get_output_filename(
                resource['dataset_info']['name'], '', opts=['right']))
        left_jpg = lbase+'jpg'
        right_jpg = rbase+'jpg'
        left_tiff = lbase+'tif'
        right_tiff = rbase+'tif'

        # Get left/right files and metadata
        for fname in resource['local_paths']:
            if fname.endswith('_dataset_metadata.json'):
                md = bin2tiff.load_json(fname)
                for m in md:
                    if 'content' in m and 'lemnatec_measurement_metadata' in m['content']:
                        metadata = bin2tiff.lower_keys(m['content'])
                        break
            elif fname.endswith('_left.bin'):
                img_left = fname
            elif fname.endswith('_right.bin'):
                img_right = fname
        if None in [img_left, img_right, metadata]:
            raise ValueError("could not locate each of left+right+metadata in processing")

        uploaded_file_ids = []

        logging.info("...determining image shapes")
        left_shape = bin2tiff.get_image_shape(metadata, 'left')
        right_shape = bin2tiff.get_image_shape(metadata, 'right')
        (left_gps_bounds, right_gps_bounds) = terrautils.extractors.calculate_gps_bounds(metadata)
        out_tmp_tiff = "/home/extractor/"+resource['dataset_info']['name']+".tif"

        skipped_jpg = False
        if (not os.path.isfile(left_jpg)) or self.force_overwrite:
            logging.info("...creating & uploading left JPG")
            left_image = bin2tiff.process_image(left_shape, img_left, None)
            terrautils.extractors.create_image(left_image, left_jpg)
            # Only upload the newly generated file to Clowder if it isn't already in dataset
            if left_jpg not in resource['local_paths']:
                fileid = pyclowder.files.upload_to_dataset(connector, host, secret_key, resource['id'], left_jpg)
                uploaded_file_ids.append(fileid)
            created += 1
            bytes += os.path.getsize(left_jpg)
        else:
            skipped_jpg = True

        if (not os.path.isfile(left_tiff)) or self.force_overwrite:
            logging.info("...creating & uploading left geoTIFF")
            if skipped_jpg:
                left_image = bin2tiff.process_image(left_shape, img_left, None)
            # Rename output.tif after creation to avoid long path errors
            terrautils.extractors.create_geotiff(left_image, left_gps_bounds, out_tmp_tiff)
            shutil.move(out_tmp_tiff, left_tiff)
            if left_tiff not in resource['local_paths']:
                fileid = pyclowder.files.upload_to_dataset(connector, host, secret_key, resource['id'], left_tiff)
                uploaded_file_ids.append(fileid)
            created += 1
            bytes += os.path.getsize(left_tiff)
        del left_image

        skipped_jpg = False
        if (not os.path.isfile(right_jpg)) or self.force_overwrite:
            logging.info("...creating & uploading right JPG")
            right_image = bin2tiff.process_image(right_shape, img_right, None)
            terrautils.extractors.create_image(right_image, right_jpg)
            if right_jpg not in resource['local_paths']:
                fileid = pyclowder.files.upload_to_dataset(connector, host, secret_key, resource['id'], right_jpg)
                uploaded_file_ids.append(fileid)
            created += 1
            bytes += os.path.getsize(right_jpg)
        else:
            skipped_jpg = True

        if (not os.path.isfile(right_tiff)) or self.force_overwrite:
            logging.info("...creating & uploading right geoTIFF")
            if skipped_jpg:
                right_image = bin2tiff.process_image(right_shape, img_right, None)
            terrautils.extractors.create_geotiff(right_image, right_gps_bounds, out_tmp_tiff)
            shutil.move(out_tmp_tiff, right_tiff)
            if right_tiff not in resource['local_paths']:
                fileid = pyclowder.files.upload_to_dataset(connector, host, secret_key, resource['id'],right_tiff)
                uploaded_file_ids.append(fileid)
            created += 1
            bytes += os.path.getsize(right_tiff)
        del right_image


        # Remove existing metadata from this extractor before rewriting
        md = pyclowder.datasets.download_metadata(connector, host, secret_key, resource['id'], self.extractor_info['name'])
        for m in md:
            if 'agent' in m and 'name' in m['agent']:
                if m['agent']['name'].endswith(self.extractor_info['name']):
                    if 'files_created' in m['content']:
                        uploaded_file_ids += m['content']['files_created']
                    pyclowder.datasets.remove_metadata(connector, host, secret_key, resource['id'], self.extractor_info['name'])

        # Tell Clowder this is completed so subsequent file updates don't daisy-chain
        metadata = terrautils.extractors.build_metadata(host, self.extractor_info['name'], resource['id'], {
                "files_created": uploaded_file_ids
            }, 'dataset')
        pyclowder.datasets.upload_metadata(connector, host, secret_key, resource['id'], metadata)

        endtime = datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
        terrautils.extractors.log_to_influxdb(self.extractor_info['name'], self.influx_params,
                                              starttime, endtime, created, bytes)

if __name__ == "__main__":
    extractor = StereoBin2JpgTiff()
    extractor.start()
