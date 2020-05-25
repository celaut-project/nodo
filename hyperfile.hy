FROM 6
CTR None
API None
PKG git clone https://github.com/josemibnf/sat-solver.git
IMPORT 16

.OCI
:FROM python:3.6
:RUN apt-get update \
:    && apt-get -y install python3-tk \
:    && apt-get -y install python3-pip \
:    && apt-get -y install minisat \
:    && pip3 install pandas \
:    && pip3 install numpy \

.HYPER
:FROM 22
:API javaAPI
:PKG npm treasureworld
:CTR None
:
:.OCI
::FROM java:11