from pip._vendor import requests
from asyncio.runners import run

if __name__ == "__main__":
    solver_image = "3723c39d43fc"
    tester_image = "3oi4nk4rmjkmkln"
    params = ( "cnf" , tester_image )
    run('docker run ',solver_image, " --params ",params)