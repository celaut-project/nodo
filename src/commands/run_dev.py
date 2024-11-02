from src.gateway.launcher.local_execution.dev_container import create_dev_container

def run_dev(path: str):
    print(f"Executing container for development purposes, project: {path}")
    create_dev_container(service_path=path)