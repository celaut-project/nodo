# celaut
python3 -m grpc_tools.protoc -I. --python_out=. protos/celaut.proto --experimental_allow_proto3_optional &&
# gateway
python3 -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. protos/gateway.proto --experimental_allow_proto3_optional &&
# compile
python3 -m grpc_tools.protoc -I. --python_out=. protos/compile.proto --experimental_allow_proto3_optional