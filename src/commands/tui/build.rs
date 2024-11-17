use std::fs;

fn main() {
    // Tells cargo to rerun the build script if the .proto files change.
    println!("cargo:rerun-if-changed=protos/celaut.proto");

    fs::create_dir_all("src/protos").expect("Failed to create protos directory");

    // Tell prost-build where to find the .proto file and output the Rust file.
    prost_build::Config::new()
        .out_dir("src/protos")
        .compile_protos(&["protos/celaut.proto"], &["protos"])
        .expect("Failed to compile protobuf files");
}
