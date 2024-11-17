fn main() {
    // Tells cargo to rerun the build script if the .proto files change.
    println!("cargo:rerun-if-changed=../../../protos/celaut.proto");

    // Tell prost-build where to find the .proto file and output the Rust file.
    prost_build::Config::new()
        .out_dir("../../../protos")
        .compile_protos(&["../../../protos/celaut.proto"], &["src"])
        .expect("Failed to compile protobuf files");
}
