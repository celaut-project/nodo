# ipss
python3 -m grpc_tools.protoc -I. --python_out=. ipss.proto --experimental_allow_proto3_optional &&
# gateway
python3 -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. gateway.proto --experimental_allow_proto3_optional