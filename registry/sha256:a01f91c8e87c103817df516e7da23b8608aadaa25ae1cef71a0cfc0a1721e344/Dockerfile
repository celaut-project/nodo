FROM ubuntu
RUN apt-get update --fix-missing && apt-get -y install python3.6 && apt-get clean
RUN apt-get update --fix-missing && apt-get -y install python3-tk && apt-get clean
RUN apt-get update --fix-missing && apt-get -y install python3-pip && apt-get clean
RUN pip3 install flask
RUN apt-get update --fix-missing && apt-get -y install wget && apt-get clean
RUN wget https://github.com/josemibnf/Solvers/archive/frontier.tar.gz && tar -xvzf frontier.tar.gz