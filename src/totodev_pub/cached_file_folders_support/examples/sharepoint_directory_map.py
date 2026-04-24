#!/usr/bin/env python3
# Part of the totodev_pub library.
# Repository: https://github.com/ToTo-LLC/totodev-pub

"""
SharePoint Directory Map

Explore a SharePoint folder hierarchy via Microsoft Graph and render a simple
tree view with per-folder file counts.

Required env: SHAREPOINT_CLIENT_ID, SHAREPOINT_CLIENT_SECRET, SHAREPOINT_TENANT_ID,
SHAREPOINT_SITE_NAME, SHAREPOINT_DOMAIN, SHAREPOINT_DRIVE_ID

Usage:
    python sharepoint_directory_map.py --target-folder "Documents/Projects"
    python sharepoint_directory_map.py --target-folder "" --max-depth 2 # Root directory

Example Output:
    Mapping SharePoint folder: Documents/Projects
    Directory:
    📁 Documents/Projects
    ├── 📁 Client A
    │   ├── 📁 Reports
    │   │   └── 📁 2024
    │   │       📊 5 files in this folder
    │   ├── 📁 Contracts
    │   │   📊 3 files in this folder
    │   📊 2 files in this folder
    ├── 📁 Client B
    │   ├── 📁 Deliverables
    │   │   📊 8 files in this folder
    │   📊 1 files in this folder
    └── 📁 Archive
        📊 12 files in this folder

Note: this program could be made much faster by using parallel async calls to the API or by using Microsoft Graph's Batch API.
We have not done this in order to keep the code simple and easy to understand.
"""

import sys
import os
import logging

import click
from msal import ConfidentialClientApplication
import requests


from totodev_pub.cached_file_folders_support.file_proxy_sharepoint import SharepointFileProxyFactory

def configure_logging(debug_enabled: bool = False):
    """Set concise logging levels for external libraries."""
    for logger_name in ['msal', 'urllib3', 'requests']:
        logging.getLogger(logger_name).setLevel(logging.DEBUG if debug_enabled else logging.WARNING)

def validate_environment() -> dict:
    """Return lower-cased SharePoint config or exit with a concise error."""
    required = ['SHAREPOINT_CLIENT_ID', 'SHAREPOINT_CLIENT_SECRET', 'SHAREPOINT_TENANT_ID',
                'SHAREPOINT_SITE_NAME', 'SHAREPOINT_DOMAIN', 'SHAREPOINT_DRIVE_ID']
    missing = [var for var in required if not os.getenv(var)]
    if missing:
        click.echo(f"❌ Missing environment variables: {', '.join(missing)}", err=True)
        sys.exit(1)
    return {var.lower(): os.getenv(var) for var in required}

def get_access_token(config: dict) -> str:
    """Acquire app-only access token via MSAL client credentials."""
    app = ConfidentialClientApplication(
        config['sharepoint_client_id'], 
        authority=f"https://login.microsoftonline.com/{config['sharepoint_tenant_id']}", 
        client_credential=config['sharepoint_client_secret']
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    
    if "access_token" not in result:
        error_msg = result.get('error_description', 'Unknown authentication error')
        raise RuntimeError(f"Failed to get access token: {error_msg}")
    
    return result["access_token"]

def get_site_info(access_token: str, config: dict) -> tuple[str, str]:
    """Return (site_id, drive_id) using domain/site lookup and configured drive id."""
    headers = {'Authorization': f'Bearer {access_token}'}
    
    site_url = f"https://graph.microsoft.com/v1.0/sites/{config['sharepoint_domain']}:/sites/{config['sharepoint_site_name']}"
    response = requests.get(site_url, headers=headers)
    response.raise_for_status()
    site_data = response.json()
    site_id = site_data['id']
    
    drive_id = config['sharepoint_drive_id']
    
    return site_id, drive_id

def build_directory_tree(factory, folder_id: str = None, folder_name: str = "root", max_depth: int = 10, current_path: str = "", is_root: bool = True) -> dict:
    """Recursively build a folder-only tree with file counts using /children."""
    if is_root:
        full_path = folder_name
    elif current_path:
        full_path = f"{current_path}/{folder_name}"
    else:
        full_path = folder_name
    
    if not is_root:
        click.echo(f"  {full_path}")
    
    tree = {'name': folder_name, 'type': 'folder', 'children': [], 'file_count': 0}

    # Respect depth limit: when max_depth <= 0, do not traverse further
    if max_depth <= 0:
        return tree
    
    try:
        headers = {'Authorization': f'Bearer {factory.access_token}'}
        
        if folder_id is None:
            url = f"{factory.base_url}/drives/{factory.drive_id}/root/children"
        else:
            url = f"{factory.base_url}/drives/{factory.drive_id}/items/{folder_id}/children"
        
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            
            for item in data.get('value', []):
                if 'folder' in item:
                    child_folder_name = item.get('name', item.get('displayName', 'Unknown'))
                    child_folder_id = item['id']
                    child_tree = build_directory_tree(factory, child_folder_id, child_folder_name, max_depth - 1, full_path, is_root=False)
                    tree['children'].append(child_tree)
                else:
                    tree['file_count'] += 1
        else:
            tree['error'] = f"HTTP error {response.status_code}: {response.text}"
    
    except Exception as e:
        tree['error'] = str(e)
    
    return tree

def display_tree(tree: dict, prefix: str = "", is_last: bool = True, max_depth: int = 10, current_depth: int = 0) -> None:
    """Print a simple tree (folders only) with file counts per folder."""
    if current_depth > max_depth:
        return
    
    if current_depth == 0:
        click.echo(f"📁 {tree['name']}")
    else:
        connector = "└── " if is_last else "├── "
        icon = "📁" if tree['type'] == 'folder' else "📄"
        click.echo(f"{prefix}{connector}{icon} {tree['name']}")
    
    if 'error' in tree:
        click.echo(f"{prefix}    ❌ Error: {tree['error']}", err=True)
        return
    
    if tree['type'] == 'folder' and tree['children']:
        children = tree['children']
        children.sort(key=lambda x: x['name'].lower())
        
        for i, child in enumerate(children):
            is_last_child = (i == len(children) - 1)
            child_prefix = prefix + ("    " if is_last else "│   ")
            
            if child['type'] == 'folder':
                display_tree(child, child_prefix, is_last_child, max_depth, current_depth + 1)
    
    if tree['type'] == 'folder' and tree['file_count'] > 0:
        click.echo(f"{prefix}    📊 {tree['file_count']} files in this folder")

@click.command()
@click.option('--target-folder', required=True, help='SharePoint folder path to map (use "" or "/" for root)')
@click.option('--max-depth', default=3, help='Maximum folder depth to traverse')
@click.option('--debug', is_flag=True, help='Enable debug logging from external libraries')
def main(target_folder: str, max_depth: int, debug: bool):
    """Map a SharePoint folder structure and print a tree view."""
    configure_logging(debug)
    config = validate_environment()
    access_token = get_access_token(config)
    site_id, drive_id = get_site_info(access_token, config)

    factory = SharepointFileProxyFactory(
        site_id=site_id,
        drive_id=drive_id,
        access_token=access_token,
        site_name=config['sharepoint_site_name']
    )

    if target_folder in ["", "/", "root"]:
        scan_path = "root"
        click.echo("Mapping SharePoint root directory")
    else:
        scan_path = target_folder
        click.echo(f"Mapping SharePoint folder: {target_folder}")

    tree = build_directory_tree(factory, folder_id=None, folder_name=scan_path, max_depth=max_depth)
    click.echo("Directory:")
    display_tree(tree, max_depth=max_depth)

if __name__ == "__main__":
    main()
