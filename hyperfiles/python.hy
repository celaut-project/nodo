PKG0 10 #ubuntu
PKG1 16 #python
IMPORT
CTR none
API none
BUILD
TENSOR
LEDGER

. #ubuntu
:@hash256@
:http:\\www.docker.com\ubuntu:18.0.oci
:http:\\www.ocirepo.rethat.com\ubuntu:18.0.oci
:@AfterAll

. #python
:@BeforeAll
:@hash256@
:apt-get install python3.8
:@AfterAll