use std::fs;

// The TUI compiles the repository's ONE canonical proto, at ../../../protos. It used
// to keep its own copy under tui/protos, which silently drifted: `Service.Api.slot`
// ended up as field 4 here and field 1 in the canonical schema, so the two spoke
// incompatible wire formats while `protos/README.md` declared them identical. A
// second copy of a wire contract has no way to stay honest; there is now only one.
const PROTO_DIR: &str = "../../../protos";

fn main() {
    // Tells cargo to rerun the build script if the .proto files change.
    println!("cargo:rerun-if-changed={PROTO_DIR}/celaut.proto");
    println!("cargo:rerun-if-changed={PROTO_DIR}/buffer.proto");

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
        .compile_protos(&[format!("{PROTO_DIR}/celaut.proto")], &[PROTO_DIR])
        .expect("Failed to compile protobuf files");
}
