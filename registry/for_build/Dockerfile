FROM ubuntu
RUN apt-get update --fix-missing && apt-get -y install python3.6
RUN apt-get update --fix-missing && apt-get -y install python3-tk
RUN apt-get update --fix-missing && apt-get -y install python3-pip
RUN pip3 install flask
RUN mkdir /random && cd /random && apt-get update && apt-get -y install git && git clone https://github.com/josemibnf/randomCNF.git && mv randomCNF/* . && rm -rf randomCNF  && rm -rf .git
ENTRYPOINT python3 start.py
WORKDIR /random/