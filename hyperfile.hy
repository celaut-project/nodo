FROM scratch
CTR None
API None
PKG docker get Python:3.6
RUN apt-get update \
    && apt-get -y install python3-tk \
    && apt-get -y install python3-pip \
    && apt-get -y install minisat \
    && pip3 install pandas \
    && pip3 install numpy \
PKG git clone www.github.com/josemibnf/sat-solver