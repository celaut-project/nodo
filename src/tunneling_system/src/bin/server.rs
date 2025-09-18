// server.rs
use anyhow::{Context, Result};
use quinn::{Endpoint, ServerConfig};
use rcgen::generate_simple_self_signed;
use std::{net::SocketAddr, sync::Arc};
use tokio::{io::{AsyncReadExt, AsyncWriteExt}, net::TcpStream};
use std::collections::HashSet;

#[tokio::main]
async fn main() -> Result<()> {
    tracing_subscriber::fmt::init();

    // Listen UDP socket for QUIC
    let bind_addr: SocketAddr = "0.0.0.0:4433".parse()?;
    let (endpoint, mut incoming) = make_server_endpoint(bind_addr)?;

    println!("QUIC server listening on {}", bind_addr);

    // accept incoming connections
    while let Some(conn) = incoming.next().await {
        tokio::spawn(handle_connection(conn));
    }

    // never reached
    let _ = endpoint;
    Ok(())
}

async fn handle_connection(connecting: quinn::Connecting) -> Result<()> {
    let connection = connecting.await.context("accept connection")?;
    println!("New connection: remote={}", connection.remote_address());

    let mut bi_streams = connection.accept_bi();

    // accept bidirectional streams (one stream == one proxied TCP connection)
    while let Some(stream_res) = bi_streams.next().await {
        match stream_res {
            Ok((mut recv, mut send)) => {
                // spawn a task to handle this stream
                tokio::spawn(async move {
                    if let Err(e) = handle_stream(&mut recv, &mut send).await {
                        eprintln!("stream handling error: {:#}", e);
                    }
                });
            }
            Err(e) => {
                eprintln!("accept_bi error: {}", e);
                break;
            }
        }
    }

    println!("Connection closed: {}", connection.remote_address());
    Ok(())
}

/// Protocol per-stream:
/// Client opens a bi-directional stream and first sends:
///   "CONNECT <ip:port>\n"
/// Then the rest of bytes are forwarded raw between the stream and the TCP socket.
async fn handle_stream(
    recv: &mut quinn::RecvStream,
    send: &mut quinn::SendStream,
) -> Result<()> {
    // read until newline to get target
    let mut target = Vec::new();
    loop {
        let mut buf = [0u8; 1];
        let n = recv.read_exact(&mut buf).await?;
        if n == 0 { break; }
        target.push(buf[0]);
        if buf[0] == b'\n' { break; }
        // safety: limit header length
        if target.len() > 256 {
            anyhow::bail!("connect header too long");
        }
    }
    let header = String::from_utf8_lossy(&target);
    let header = header.trim_end(); // remove newline

    // expecting: "CONNECT ip:port"
    let mut parts = header.split_whitespace();
    let cmd = parts.next().unwrap_or("");
    if cmd.to_uppercase() != "CONNECT" {
        anyhow::bail!("expected CONNECT header, got '{}'", header);
    }
    let addr = parts.next().context("no target in CONNECT")?;
    println!("Stream: request to connect to {}", addr);

    // connect via TCP to target
    let mut tcp = TcpStream::connect(addr).await.context("connect to target failed")?;
    // set TCP nodelay for better latency
    let _ = tcp.set_nodelay(true);

    // now forward both directions:
    // - QUIC recv -> TCP write
    // - TCP read -> QUIC send
    let mut tcp_read = tcp.clone();
    let mut tcp_write = tcp;

    // spawn task TCP -> QUIC
    let mut send_clone = send.clone();
    let tcp_to_quic = async move {
        let mut buf = [0u8; 8192];
        loop {
            let n = tcp_read.read(&mut buf).await?;
            if n == 0 {
                // TCP closed, finish send
                send_clone.finish().await?;
                break;
            }
            send_clone.write_all(&buf[..n]).await?;
        }
        Ok::<(), anyhow::Error>(())
    };

    // QUIC -> TCP
    let quic_to_tcp = async move {
        let mut buf = [0u8; 8192];
        loop {
            let n = recv.read(&mut buf).await?;
            if n == 0 {
                // remote closed
                break;
            }
            tcp_write.write_all(&buf[..n]).await?;
        }
        Ok::<(), anyhow::Error>(())
    };

    // run both directions concurrently
    tokio::try_join!(tcp_to_quic, quic_to_tcp)?;

    println!("Stream proxied and closed for {}", addr);
    Ok(())
}

fn make_server_endpoint(bind: SocketAddr) -> Result<(Endpoint, quinn::Incoming)> {
    // generate self-signed cert
    let cert = generate_simple_self_signed(vec!["localhost".into()])?;
    let key = quinn::PrivateKey::from_der(&cert.serialize_private_key_der())?;
    let cert_der = cert.serialize_der()?;
    let cert_chain = quinn::Certificate::from_der(&cert_der)?;
    let mut server_config = ServerConfig::with_single_cert(vec![cert_chain], key)?;
    Arc::get_mut(&mut server_config.transport)
        .unwrap()
        .max_concurrent_bidi_streams(1024u32.into());

    let mut endpoint = Endpoint::server(server_config, bind)?;
    let incoming = endpoint.incoming();
    Ok((endpoint, incoming))
}
