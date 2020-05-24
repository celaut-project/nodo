FROM scratch
CTR None
PKG docker Python
RUN apt-get update \
    && apt-get -y install python3-tk \
    && apt-get -y install python3-pip \
    && apt-get -y install minisat \
    && pip3 install pandas \
    && pip3 install numpy \
PKG www.github.com/josemibnf/sat-solver