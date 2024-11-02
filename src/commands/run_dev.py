from src.gateway.launcher.local_execution.dev_container import create_dev_container

def run_dev(path: str):
    print(f"Execute container for development purpouse, project: {path}")
    create_dev_container(service_path=path)