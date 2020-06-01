\FROM ubuntu
\
\RUN apt-get update \
\    && apt-get -y install python3.6
\
\RUN apt-get update \
\    && apt-get -y install python3-tk \
\    && apt-get -y install python3-pip \
\    && apt-get -y install minisat \
\    && pip3 install pandas \
\    && pip3 install numpy
\
\WORKDIR /frontier/
\RUN apt-get update \
\    && apt-get -y install git \
\    && git clone --branch frontier https://github.com/josemibnf/sat-solver.git \
\    && mv sat-solver/* . \
\    && rm -rf sat-solver