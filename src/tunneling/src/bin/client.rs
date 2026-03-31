// client.rs
use anyhow::{Context, Result};
use quinn::{ClientConfig, Endpoint};
use rcgen::generate_simple_self_signed;
use std::net::SocketAddr;
use tokio::{io::{self, AsyncReadExt, AsyncWriteExt}};

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt::init();

    // server addr
    let server: SocketAddr = "127.0.0.1:4433".parse()?;

    // build a config that accepts the self-signed cert we generated on server
    let mut client_cfg = ClientConfig::with_native_roots();
    // for testing, accept any certificate (INSECURE) -- production don't do this.
    client_cfg.crypto.set_verify_certificate_callback(Arc::new(|_cert, _| {
        // accept
        futures::future::ready(Ok(()))
    }));

    let mut endpoint = Endpoint::client("0.0.0.0:0".parse::<SocketAddr>()?)?;
    endpoint.set_default_client_config(client_cfg);

    let new_conn = endpoint.connect(server, "localhost")?.await.context("connect failed")?;
    println!("connected: addr={}", new_conn.connection.remote_address());

    // Example interactive mode: read lines from stdin, each line is a "CONNECT ip:port" command.
    // After issuing CONNECT, the client will open a stream and forward stdin/stdout through it.
    println!("Enter a target like: 127.0.0.1:8080");
    let mut line = String::new();
    let stdin = io::stdin();
    let mut stdin_read = stdin;

    loop {
        line.clear();
        let n = stdin_read.read_line(&mut line).await?;
        if n == 0 { break; }
        let target = line.trim();
        if target.is_empty() { continue; }

        // Open a bi stream for this proxied connection
        let (mut send, mut recv) = new_conn.connection.open_bi().await?;
        // send CONNECT header
        let header = format!("CONNECT {}\n", target);
        send.write_all(header.as_bytes()).await?;
        // spawn task to read from QUIC stream and write to stdout
        let mut recv_clone = recv.clone();
        tokio::spawn(async move {
            let mut stdout = io::stdout();
            let mut buf = [0u8; 8192];
            loop {
                let n = match recv_clone.read(&mut buf).await {
                    Ok(n) => n,
                    Err(_) => { break; }
                };
                if n == 0 { break; }
                if let Err(e) = stdout.write_all(&buf[..n]).await {
                    eprintln!("stdout write err: {}", e);
                    break;
                }
            }
        });

        // now forward data from stdin into the QUIC send stream
        // (for demo, we'll just send a simple HTTP GET and then finish)
        let http_get = format!("GET / HTTP/1.0\r\nHost: {}\r\n\r\n", target);
        send.write_all(http_get.as_bytes()).await?;
        send.finish().await?;
        println!("Sent request to {} on a QUIC stream; awaiting response...", target);
    }

    Ok(())
}
