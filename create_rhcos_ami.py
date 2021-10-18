#!/usr/bin/env python3

import gzip
import logging
import logging.config
import os
import re
import shutil
import tempfile
import time

import boto3
from bs4 import BeautifulSoup
import click
import requests

###############################################################################
# TODO
#
# Determine region of bucket to use for API calls to create AMI
# Take parameter for copying AMI to other regions
# Pass parameters into each function for RHCOS release instead of relying on class variables
#
###############################################################################

LOGGING_CONFIG = {
    'version': 1,
    'formatters': {
        'simple': {
            'format':
                '%(asctime)-8s | %(levelname)-8s | %(name)-10s | %(message)s',
            'datefmt': '%H:%M:%S',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'level': os.environ.get('OPENSHIFT4_UTILS_LOGLEVEL', 'INFO'),
            'formatter': 'simple',
        },
    },
    'loggers': {
        'app': {
            'level': os.environ.get('OPENSHIFT4_UTILS_LOGLEVEL', 'INFO'),
            'handlers': [
                'console',
            ],
            'propagate': 'no',
        },
    },
}

logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger('app')

class Base():
    def __init__(self) -> None:
        self._base_url = 'http://mirror.openshift.com/pub/openshift-v4/dependencies/rhcos'


class RHCOSRelease(Base):
    def __init__(self, version) -> None:
        super().__init__()

        self.version = version
        self.version_x = re.search(r'(^\d)', self.version).group(1)
        self.version_y = re.search(r'(^\d\.\d)', self.version).group(1)

        self.filename = f'rhcos-{self.version}-x86_64-aws.x86_64.vmdk'
        self.filename_gzip = f'rhcos-{self.version}-x86_64-aws.x86_64.vmdk.gz'

        self.download_url = f'{self._base_url}/{self.version_y}/{self.version}/{self.filename_gzip}'
        self.download_path = os.path.join(tempfile.gettempdir(), self.filename_gzip)
        self.unpack_path = os.path.join(tempfile.gettempdir(), self.filename)

    def __repr__(self) -> str:
        return f'RHCOSRelease({self.version})'

    def download(self) -> None:
        """Download the RHCOS gzip file and save it to the temp directory."""
        if os.path.exists(self.download_path):
            logger.info(f'Skipping download, {self.download_path} already exists')
            return

        logger.info(f'Downloading {self.download_url}')

        r = requests.get(self.download_url)
        with open(self.download_path, 'wb') as f:
            logger.info(f'Saving {self.download_path}')
            f.write(r.content)

    def unpack(self) -> None:
        """Unpack the downloaded RHCOS gzip file in the same directory."""
        if os.path.exists(self.unpack_path):
            logger.info(f'Skipping unpack, {self.unpack_path} already exists')
            return

        if not os.path.exists(self.download_path):
            self.download()

        logger.info(f'Unpacking {self.download_path}')

        with gzip.open(self.download_path, 'rb') as f_in:
            with open(self.unpack_path, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)

    def upload(self, s3_bucket) -> None:
        """Upload the unpacked RHCOS image to S3."""
        s3 = boto3.client('s3')

        if s3.list_objects_v2(Bucket=s3_bucket, Prefix=self.filename).get('KeyCount', 0) > 0:
            logger.info(f'Skipping upload, s3://{s3_bucket}/{self.filename} already exists')
            return

        if not os.path.exists(self.unpack_path):
            self.unpack()

        with open(self.unpack_path, 'rb') as f:
            logger.info(f'Uploading {self.unpack_path} to s3://{s3_bucket}/{self.filename}')
            s3.upload_fileobj(f, s3_bucket, self.filename)

    def existing_snapshot(self) -> str:
        """Checks for existing snapshot and returns the snapshot ID if it exists."""
        ec2 = boto3.client('ec2')
        existing_snapshots = ec2.describe_snapshots(
            Filters=[
                {
                    'Name': 'tag:rhcos_version',
                    'Values': [self.version],
                }
            ],
            OwnerIds=['self'],
        )

        if len(existing_snapshots['Snapshots']) > 0:
            return existing_snapshots['Snapshots'][0]['SnapshotId']

    def import_snapshot(self, s3_bucket) -> str:
        """Imports a snapshot from the RHCOS image in S3."""
        snapshot_id = self.existing_snapshot()
        if snapshot_id:
            logger.info(f'Skipping snapshot import, {snapshot_id} already exists')
            return snapshot_id

        s3 = boto3.client('s3')
        if not s3.list_objects_v2(Bucket=s3_bucket, Prefix=self.filename).get('KeyCount', 0) > 0:
            self.upload(s3_bucket)

        logger.info(f'Importing snapshot from s3://{s3_bucket}/{self.filename}')

        ec2 = boto3.client('ec2')
        description = 'rhcos-{}'.format(self.version)
        import_task_id = ec2.import_snapshot(
            Description=description,
            DiskContainer={
                'Description': description,
                'Format': 'vmdk',
                'UserBucket': {
                    'S3Bucket': s3_bucket,
                    'S3Key': self.filename,
                },
            },
        )['ImportTaskId']

        max_time = 10
        timeout = time.time() + (60 * max_time)
        while True:
            logger.info('Checking status of snapshot import task {}'.format(import_task_id))
            snapshot_task = ec2.describe_import_snapshot_tasks(
                ImportTaskIds=[import_task_id],
            )

            if snapshot_task['ImportSnapshotTasks'][0]['SnapshotTaskDetail']['Status'] == 'completed':
                snapshot_id = snapshot_task['ImportSnapshotTasks'][0]['SnapshotTaskDetail']['SnapshotId']

                logger.info(f'Created snapshot {snapshot_id}')
                logger.info(f'Tagging snapshot {snapshot_id} with rhcos_version={self.version}')

                ec2.create_tags(
                    Resources=[snapshot_id],
                    Tags=[
                        {
                            'Key': 'rhcos_version',
                            'Value': self.version,
                        }
                    ],
                )

                return snapshot_id

            if time.time() > timeout:
                raise RuntimeError(f'Snapshot import task {import_task_id} took longer than {max_time} minutes')

            logger.info(f'Snapshot import task {import_task_id} not complete, checking again in 10 seconds')
            time.sleep(10)

    def existing_image(self) -> str:
        """Checks for existing image and returns the image ID if it exists."""
        ec2 = boto3.client('ec2')
        existing_images = ec2.describe_images(
            Filters=[
                {
                    'Name': 'name',
                    'Values': ['rhcos-{}'.format(self.version)],
                }
            ],
            Owners=['self'],
        )

        if len(existing_images['Images']) > 0:
            return existing_images['Images'][0]['ImageId']

    def register_image(self, public=False):
        """Registers an image from a snapshot and makes it public."""
        image_id = self.existing_image()
        if image_id:
            logger.info(f'Skipping register image, {image_id} already exists')
            return image_id

        snapshot_id = self.existing_snapshot()

        logger.info(f'Registering image from {snapshot_id}')

        ec2 = boto3.client('ec2')
        image_id = ec2.register_image(
            Name='rhcos-{}'.format(self.version),
            Description='OpenShift 4 {}'.format(self.version),
            Architecture='x86_64',
            BlockDeviceMappings=[
                {
                    'DeviceName': '/dev/xvda',
                    'Ebs': {
                        'SnapshotId': snapshot_id,
                        'DeleteOnTermination': True,
                        'VolumeType': 'gp2',
                    }
                },
                {
                    'DeviceName': '/dev/xvdb',
                    'VirtualName': 'ephemeral0',
                },
            ],
            EnaSupport=True,
            RootDeviceName='/dev/xvda',
            SriovNetSupport='simple',
            VirtualizationType='hvm',
        )['ImageId']

        logger.info('Created image {}'.format(image_id))

        if public:
            logger.info('Making image {} public'.format(image_id))
            ec2.modify_image_attribute(
                ImageId=image_id,
                LaunchPermission={
                    'Add': [
                        {
                            'Group': 'all',
                        },
                    ],
                }
            )

        return image_id

    def create_ami(self, s3_bucket, public) -> str:
        image_id = self.existing_image()
        if image_id:
            logger.info(f'RHCOS {self.version} AMI {image_id} already exists')
            return image_id

        self.download()
        self.unpack()
        self.upload(s3_bucket)
        self.import_snapshot(s3_bucket)
        return self.register_image(public)

class OpenShiftRelease(Base):
    def __init__(self, version) -> None:
        super().__init__()

        self.version = version

    def __repr__(self) -> str:
        return f'OpenShiftRelease({self.version})'

    @property
    def rhcos_releases(self) -> list:
        if not hasattr(self, '_rhcos_releases'):
            logger.info(f'Finding RHCOS releases for OpenShift {self.version}')

            self._rhcos_releases = []

            r = requests.get(f'{self._base_url}/{self.version}/')
            soup = BeautifulSoup(r.text, 'html.parser')

            # find the table cells with the RHCOS version numbers
            for row in soup.find_all('tr'):
                if len(row.contents) > 2:
                    m = re.search(r'(\d\.\d\.\d)/', row.contents[1].text)
                    if m:
                        self._rhcos_releases.append(RHCOSRelease(m.group(1)))

            logger.info(f'Found RHCOS releases {", ".join([i.version for i in self._rhcos_releases])}')

        return self._rhcos_releases


@click.command()
@click.option('--s3-bucket', required=True, help='Name of S3 bucket to upload disk images')
@click.option('--public/--no-public', default=False, help='Set permissions on AMIs as public or private')
@click.argument('ocp_versions', nargs=-1)
def create(s3_bucket, public, ocp_versions):
    """Create RHCOS AMIs for the given OCP_VERSIONS.

    Finds the RHCOS releses for the given OCP_VERSIONS and creates AMIs for each of them.
    """
    image_ids = []
    for ocp_version in ocp_versions:
        ocp_release = OpenShiftRelease(ocp_version)
        for rhcos_release in ocp_release.rhcos_releases:
            logger.info(f'Processing RHCOS release {rhcos_release.version}')
            image_id = rhcos_release.create_ami(s3_bucket, public)
            image_ids.append((rhcos_release, image_id,))

    for i in image_ids:
        print(f'- `rhcos-{i[0]} => {i[1]}')


if __name__ == '__main__':
    create()
