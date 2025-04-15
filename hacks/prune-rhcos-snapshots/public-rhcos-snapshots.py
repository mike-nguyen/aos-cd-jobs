#!/usr/bin/env python3
import os

import boto3
import re
import yaml
from typing import Dict, List, Set
import datetime
import urllib3
import pathlib
import subprocess
import time

# Set to "true" if an AMI is selected for garbage collection
AMI_TAG_KEY_GARBAGE_COLLECT = 'garbage_collect'

# Set to "true" if an AMI is mentioned in installer reference
AMI_TAG_KEY_PRODUCTION = 'production'

# Set to "true" if an AMI to ignore CSO
AMI_TAG_KEY_CSO_IGNORE = 'cso-ignore'

Commit = str
Region = str
AmiId = str

def check_ami_public(client, ami_id):
    try:
        # Describe the AMI to check its current launch permissions
        response = client.describe_image_attribute(
            ImageId=ami_id,
            Attribute='launchPermission'
        )
        
        # Extract the launch permissions from the response
        launch_permissions = response.get('LaunchPermissions')
        
        # Check if the AMI is already public
        for permission in launch_permissions:
            if permission.get('Group') == 'all':
                return True
        
        return False
    except Exception as e:
        print(f"Error checking AMI public status: {e}")
        return False
    
def make_ami_public(client, ami_id):
    try:
        if check_ami_public(client, ami_id):
            print(f"AMI {ami_id} is already public.")
        else:
            # Modify the launch permissions of the AMI to make it public
            print(f"AMI {ami_id} is private. Setting it to public")
            response = client.modify_image_attribute(
                ImageId=ami_id,
                LaunchPermission={
                    'Add': [
                        {
                            'Group': 'all',
                        },
                    ],
                },  
            )
            print(f"AMI {ami_id} is now public.")
    except Exception as e:
        print(f"Error making AMI public: {e}")


if __name__ == '__main__':

    gopath = pathlib.Path(os.getenv('GOPATH'))
    if not gopath.is_dir():
        print('Set GOPATH env before running')
        exit(1)

    installer_git_path = gopath.joinpath('src/github.com/openshift/installer')
    if not installer_git_path.is_dir():
        print(f'Requires recent clone of github.com/openshift/installer in GOPATH: {str(installer_git_path)}')
        exit(1)

    http = urllib3.PoolManager()

    amis_in_use: Set = set()

    remote_branches_cmd = subprocess.run(
        ['git', '-C', str(installer_git_path), 'branch', '-r'],
        capture_output=True)
    remote_branches_cmd.check_returncode()
    branch_names = remote_branches_cmd.stdout.decode('utf-8').split()
    release_branches = filter(lambda branch: 'upstream/release-' in branch, branch_names)
    checked_commits = set()

    for release_branch in release_branches:
        release_branch = release_branch.strip()
        print(f'Processing branch: {release_branch}')

        for rhcos_path in ('data/data/coreos/rhcos.json', 'data/data/rhcos.json', 'data/data/coreos/scos.json'):
            gitshow_process = subprocess.run(
                ['git', '-C', str(installer_git_path), 'log', '--format=%H', release_branch, '--', rhcos_path],
                capture_output=True)
            gitshow_process.check_returncode()
            commits_to_check = set(gitshow_process.stdout.decode('utf-8').split())
            commits_to_check = commits_to_check.difference(checked_commits)

            for installer_commit in commits_to_check:
                installer_commit = installer_commit.strip()
                gitshow_process = subprocess.run(['git', '-C', str(installer_git_path), 'show', f'{installer_commit}:{rhcos_path}'], capture_output=True)
                if gitshow_process.returncode != 0:
                    print(f'File {rhcos_path} removed in {installer_commit}')
                    continue
                amis_in_file = list(re.findall('ami-[a-z0-9]+', str(gitshow_process.stdout)))
                amis_in_use.update(amis_in_file)
                checked_commits.add(installer_commit)

    client = boto3.client('ec2', region_name='us-east-1')
    all_aws_regions = [region['RegionName'] for region in client.describe_regions()['Regions']]
    ignore_amis_younger_than = datetime.datetime.now() - datetime.timedelta(days=30)
    ami_analysis = dict()
    all_amis: Set[AmiId] = set()
    private_amis: Set[AmiId] = set()
    production_to_preserve: Set[AmiId] = set()
    young_amis_to_preserve: Set[AmiId] = set()
    mislabeled_prod_images = 0
    for aws_region in all_aws_regions:
        region_ami_analysis = dict()
        ami_analysis[aws_region] = region_ami_analysis

        region_client = boto3.client('ec2', region_name=aws_region)
        ec_resource = boto3.resource('ec2', region_name=aws_region)
        print(f'Querying for AMIs in {aws_region}..')
        images = region_client.describe_images(Owners=['self']).get('Images')
        print(f'Number of images in {aws_region}: {len(images)}')
        images_left = len(images)
        for image in images:
            images_left = images_left - 1
            print(f'Images left: {images_left}')
            image_id = image['ImageId']
            image_name = image.get('Name', None)
            image_description = image.get('Description', None)
            image_creation = image['CreationDate']
            image_tags = image.get('Tags', [])
            image_tag_keys = [tag['Key'] for tag in image_tags]
            all_amis.add(image_id)

            print(f'  Checking AMI {image_id} ({image_name})')

            # images already tagged with production
            if AMI_TAG_KEY_PRODUCTION in image_tag_keys:
                production_to_preserve.add(image_id)
                print(f'    AMI is labeled with {AMI_TAG_KEY_PRODUCTION}; preserving.')
                image_resource = ec_resource.Image(image_id)
                image_resource.create_tags(
                    Tags=[
                        {
                            'Key': AMI_TAG_KEY_CSO_IGNORE,
                            'Value': 'production EBS snapshot',
                        },
                    ]
                )
                make_ami_public(region_client, image_id)
                # It should be impossible to have both the production and garbage collection tag
                if AMI_TAG_KEY_GARBAGE_COLLECT in image_tag_keys:
                    for tag_entry in image_tags:
                        if tag_entry['Key'] == AMI_TAG_KEY_GARBAGE_COLLECT:
                            print(f'{tag_entry['Key']} found in prod image {image_id} {image_name}!')
                            #exit(1)  # DEBUG DEBUG DEBUG
                            tag = ec_resource.Tag(image_id, AMI_TAG_KEY_GARBAGE_COLLECT, tag_entry['Value'])
                            tag.delete()
                            mislabeled_prod_images = mislabeled_prod_images + 1
                continue

            preserve_ami = False
            # AMI IDs are only unique within a region. For simplicity's sake, we ignore the
            # unlikely case of an AMI within the same name in our account in two different
            # regions. The worst case scenario is an unnecessary preservation.
            if image_id in amis_in_use:
                print(f'    AMI {image_id} in use without production tag. Adding tag')
                preserve_ami = True
                production_to_preserve.add(image_id)
                image_resource = ec_resource.Image(image_id)
                response = image_resource.create_tags(
                    Tags=[
                        {
                            'Key': AMI_TAG_KEY_PRODUCTION,
                            'Value': 'true',
                        },
                        {
                            'Key': AMI_TAG_KEY_CSO_IGNORE,
                            'Value': 'production EBS snapshot',
                        },
                    ]
                )
                print(f'{response}:  Added tag')
                make_ami_public(region_client, image_id)

            creation_datetime = datetime.datetime.strptime(image_creation, "%Y-%m-%dT%H:%M:%S.%fZ")

            if not preserve_ami and creation_datetime > ignore_amis_younger_than:
                print(f'AMI {image_id} will be preserved due to recent creation')
                young_amis_to_preserve.add(image_id)
                preserve_ami = True
            print(f'registering {image_id}')
            # Register pruning information for this image
            region_ami_analysis[image_id] = {
                'name': image_name,
                'description': image_description,
                'creation': image_creation,
                'tags': image_tags,
                'preserve': preserve_ami,
            }

            if not preserve_ami:
                # No payload matches this RHCOS version. Find EBS snapshots to delete.
                ebs_snapshots = list()
                for block_device_mapping in image['BlockDeviceMappings']:
                    if 'Ebs' in block_device_mapping and 'SnapshotId' in block_device_mapping['Ebs']:
                        snapshot_id = block_device_mapping['Ebs']['SnapshotId']
                        ebs_snapshots.append(snapshot_id)

                region_ami_analysis[image_id]['snapshots'] = ebs_snapshots
                print(f'AMI {image_id} is not in any commits. Associated snapshot is {snapshot_id}')
                if AMI_TAG_KEY_GARBAGE_COLLECT not in image_tag_keys:
                    image_resource = ec_resource.Image(image_id)
                    image_resource.create_tags(
                        Tags=[
                            {
                                'Key': AMI_TAG_KEY_GARBAGE_COLLECT,
                                'Value': 'true'
                            },
                        ]
                    )
                    print(f'labeled {image_id} in {aws_region}')
            else:
                print(f'erroneously tagged {image_id}: {image_name} in {aws_region}')
                # If an image was erroneously tagged, delete the tag.
                if AMI_TAG_KEY_GARBAGE_COLLECT in image_tag_keys:
                    for tag_entry in image_tags:
                        if tag_entry['Key'] == AMI_TAG_KEY_GARBAGE_COLLECT:
                            print(f'{tag_entry['Key']} found!')
                            exit(1)  # DEBUG DEBUG DEBUG
                            tag = ec_resource.Tag(image_id, AMI_TAG_KEY_GARBAGE_COLLECT, tag_entry['Value'])
                            tag.delete()
                            mislabeled_prod_images = mislabeled_prod_images + 1

            print(f'Assessed: {image_id}')
            print(yaml.dump(region_ami_analysis[image_id]))
    print(f'Found {mislabeled_prod_images} prod images with garbage collection tag')
    print(f'Found a total of {len(all_amis)} in account')
    print(f'Detected {len(young_amis_to_preserve)} young AMIs to preserve')
    print(f'Installer commits suggest {len(amis_in_use)} AMIs need to be preserved')
    print(f'Detected {len(production_to_preserve)} of those AMIs in the account')
    failed_to_find = amis_in_use.difference(production_to_preserve)
    print(f'Failed to find {len(failed_to_find)} amis')
    print(f'Missing: {failed_to_find}')

    analysis = pathlib.Path('analysis.yaml')
    analysis.write_text(yaml.dump(ami_analysis))
