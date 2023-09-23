import json
import os
import sys
from hashlib import sha3_256

from typing import Optional, Dict, Generator

import grpc
from grpcbigbuffer import client as grpcbb

from protos import celaut_pb2, compile_pb2, gateway_pb2_grpcbf, gateway_pb2_grpc
from src.database.access_functions.peers import get_peer_ids, get_peer_directions

ZIP_SOURCE_DIRECTORY = 'src'

# Local storage directories
SERVICES = '__registry__'
METADATA = '__metadata__'
BLOCKS = '__block__'

# Pre-compile json keys of service storage directories.
SERVICE_DEPENDENCIES_DIRECTORY = "service_dependencies_directory"
METADATA_DEPENDENCIES_DIRECTORY = "metadata_dependencies_directory"
BLOCKS_DIRECTORY = "blocks_directory"


def export_registry(directory: str, compile_config: Dict):
    list(map(
        lambda _reg: os.makedirs(f"{directory}/{compile_config[_reg]}"),
        [
            SERVICE_DEPENDENCIES_DIRECTORY,
            METADATA_DEPENDENCIES_DIRECTORY,
            BLOCKS_DIRECTORY
        ]
    ))

    for dependency in compile_config['dependencies'].values() \
            if type(compile_config['dependencies']) is dict else compile_config['dependencies']:

        # Move dependency' service.
        if not os.path.exists(f"{SERVICES}/{dependency}"):
            raise Exception(f"Dependency not found. {dependency}")
        os.system(f"cp -R {SERVICES}/{dependency} "
                  f"{directory}/{compile_config[SERVICE_DEPENDENCIES_DIRECTORY]}")

        # Move dependency's metadata
        if os.path.exists(f"{METADATA}/{dependency}"):
            os.system(f"cp -R {METADATA}/{dependency} "
                      f"{directory}/{compile_config[METADATA_DEPENDENCIES_DIRECTORY]}")

        # Move dependency's blocks.
        if os.path.isdir(f"{SERVICES}/{dependency}"):
            with open(f"{SERVICES}/{dependency}/_.json", 'r') as dependency_json_file:
                dependency_json = json.load(dependency_json_file)
                for _e in dependency_json:
                    if type(_e) == list:
                        block: str = _e[0]
                        if not os.path.exists(
                                f'{directory}/{compile_config[BLOCKS_DIRECTORY]}/{block}'
                        ):
                            os.system(f"cp -r {BLOCKS}/{block} "
                                      f"{directory}/{compile_config[BLOCKS_DIRECTORY]}")


def generate_service_zip(project_directory: str) -> str:
    # Remove the last character '/' from the path if it exists
    if project_directory[-1] == '/':
        project_directory = project_directory[:-1]

    # Remove the ZIP file and the destination source directory if they already exist
    os.system(f"cd {project_directory}/.service && rm .service.zip && rm -rf {ZIP_SOURCE_DIRECTORY}")

    # Define the complete path for the destination source directory
    complete_source_directory = f"{project_directory}/.service/{ZIP_SOURCE_DIRECTORY}"

    # Create the destination source directory and copy all files and folders from the project there
    os.system(f"mkdir {complete_source_directory}")

    # Read the compilation's config JSON file
    with open(f'{project_directory}/.service/pre-compile.json', 'r') as config_file:
        compile_config = json.load(config_file)

    # Copia los archivos del proyecto a complete_source_directory
    # TODO   Bug: don't work for hidden directories' files.
    os.system(f"cp -r {' '.join([os.path.join(project_directory, item) for item in compile_config['include']])} "
              f"{complete_source_directory}")

    # Add a line to the Dockerfile to copy the source files to the working directory
    with open(f'{project_directory}/.service/Dockerfile', 'a') as dockerfile:
        dockerfile.write(f'COPY {ZIP_SOURCE_DIRECTORY} /{compile_config["workdir"]}/')

    # Remove the files and directories specified in the "ignore" list from the configuration
    if 'ignore' in compile_config:
        for file in compile_config['ignore']:
            os.system(f"cd {complete_source_directory} && rm -rf {file}")

    # Add the dependencies
    export_registry(directory=complete_source_directory, compile_config=compile_config)

    if compile_config['zip']:
        os.system(f'cd {complete_source_directory} && '
                  f'zip -r services.zip'
                  f' {compile_config[SERVICE_DEPENDENCIES_DIRECTORY]}'
                  f' {compile_config[METADATA_DEPENDENCIES_DIRECTORY]}'
                  f' {compile_config[BLOCKS_DIRECTORY]}')
        os.system(f'cd {complete_source_directory} && '
                  f'rm -rf {compile_config[SERVICE_DEPENDENCIES_DIRECTORY]} '
                  f'{compile_config[METADATA_DEPENDENCIES_DIRECTORY]} '
                  f'{compile_config[BLOCKS_DIRECTORY]}')

    # Create a ZIP file of the destination source directory
    os.system(f"cd {project_directory}/.service && zip -r .service.zip .")

    # Delete the last line to the Dockerfile to copy the source files to the working directory
    os.system('sed -i "$ d" {0}'.format(f"{project_directory}/.service/Dockerfile"))

    # Remove the destination source directory
    os.system(f"rm -rf {complete_source_directory}")

    # Return the path of the generated ZIP file
    return project_directory + '/.service/.service.zip'


def _compile(zip, node: str):
    yield from grpcbb.client_grpc(
        method=gateway_pb2_grpc.GatewayStub(
            grpc.insecure_channel(node + ':8090')
        ).Compile,
        input=grpcbb.Dir(dir=zip, _type=bytes),
        indices_serializer={0: bytes},
        indices_parser=gateway_pb2_grpcbf.CompileOutput_indices,
        partitions_message_mode_parser={1: True, 2: True, 3: False}
    )


def on_peer(peer):
    _id: Optional[str] = None
    print(f'Start compile.')
    for b in _compile(
            zip=service_zip_dir,
            node=peer if len(sys.argv) == 2 else sys.argv[2]
    ):
        print('b -> ', b, type(b), b.type if type(b) is grpcbb.Dir else None)
        print(f"_id -> {_id}")
        if type(b) is compile_pb2.CompileOutputServiceId:
            if _id:
                print(f"Ya se había proporcionado el id {_id}")
            _id = b.id.hex()
            print(f"El id es {_id}")
        elif type(b) == celaut_pb2.Any.Metadata and _id:
            with open(f"__metadata__/{_id}", "wb") as f:
                f.write(b.SerializeToString())
        elif type(b) == grpcbb.Dir and b.type == compile_pb2.Service and _id:
            # b is ServiceWithMeta grpc-bb cache directory.
            os.system('mv ' + b.dir + ' ' + '__registry__/' + _id)
        else:
            raise Exception('\nError with the compiler output:' + str(b))

    os.remove(service_zip_dir)
    print('service id -> ', _id)

    print('\n Validate the content.')

    validate_id = sha3_256()

    try:
        for i in grpcbb.read_multiblock_directory('__registry__/' + _id + '/'):
            validate_id.update(i)
    except Exception as e:
        print(f"¿Tal vez no tiene bloques? {str(e)}")

    print("validated service id -> ", validate_id.hexdigest())


if __name__ == '__main__':
    if not os.path.exists('__cache__'):
        os.mkdir('__cache__')

    if not os.path.exists('__registry__'):
        os.mkdir('__registry__')

    service_zip_dir: str = generate_service_zip(
        project_directory=sys.argv[1]
    )

    [
        on_peer(peer=f"{ip}:{port}")
        for peer_id in get_peer_ids()
        for ip, port in get_peer_directions(peer_id=peer_id)
    ]
