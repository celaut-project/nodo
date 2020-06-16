FROM ubuntu
RUN apt-get update && apt-get -y install python3.6
RUN apt-get update && apt-get -y install python3-tk
RUN apt-get update && apt-get -y install python3-pip
RUN apt-get update && apt-get -y install minisat
RUN pip3 install flask
RUN mkdir /random && cd /random && apt-get update && apt-get -y install git && git clone --branch randomCNF https://github.com/josemibnf/sat-solver.git && mv sat-solver/* . && rm -rf sat-solver  && rm -rf .git
ENTRYPOINT python3 start.py
WORKDIR /random/