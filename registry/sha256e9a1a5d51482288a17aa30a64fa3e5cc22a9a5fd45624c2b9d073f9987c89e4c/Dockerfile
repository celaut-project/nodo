FROM ubuntu
RUN apt-get update --fix-missing && apt-get -y install python3.6 && apt-get clean
RUN apt-get update --fix-missing && apt-get -y install python3-tk && apt-get clean
RUN apt-get update --fix-missing && apt-get -y install python3-pip && apt-get clean
RUN pip3 install flask
RUN mkdir /random && cd /random && apt-get update && apt-get -y install git && git clone https://github.com/josemibnf/randomCNF.git && mv randomCNF/* . && rm -rf randomCNF  && rm -rf .git