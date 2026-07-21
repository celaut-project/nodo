use std::fs;

fn main() {
    // Tells cargo to rerun the build script if the .proto files change.
    println!("cargo:rerun-if-changed=protos/celaut.proto");

    fs::create_dir_all("src/protos").expect("Failed to create protos directory");

    // prost-build shells out to `protoc`. nodo hosts (and the WSL rootfs) don't
    // ship a system protoc, so `nodo tui` failed to build. Point prost-build at a
    // vendored protoc binary so the TUI builds with no external protobuf install.
    let protoc = protoc_bin_vendored::protoc_bin_path().expect("vendored protoc unavailable");
    std::env::set_var("PROTOC", protoc);

    // Tell prost-build where to find the .proto file and output the Rust file.
    prost_build::Config::new()
        .out_dir("src/protos")
        .protoc_arg("--experimental_allow_proto3_optional")
        .compile_protos(&["protos/celaut.proto"], &["protos"])
        .expect("Failed to compile protobuf files");
}
