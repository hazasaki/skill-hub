//! Pure-std loopback-only HTTP/1.1 POST client for local Skill Hub `/route_task`.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream, ToSocketAddrs};
use std::time::{Duration, Instant};

use crate::json::{parse, JsonValue};

#[derive(Debug, PartialEq, Eq)]
pub enum HttpError {
    Timeout,
    Unavailable(String),
    InvalidJson,
    InvalidShape,
    HttpStatus(u16),
}

fn parse_loopback_host_port(url: &str) -> Result<(&str, &str), HttpError> {
    let stripped = url
        .strip_prefix("http://")
        .ok_or_else(|| HttpError::Unavailable("only http:// URLs are supported".to_string()))?;
    let (host_port, path) = match stripped.find('/') {
        Some(idx) => (&stripped[..idx], &stripped[idx..]),
        None => (stripped, "/"),
    };
    if host_port.contains('@') {
        return Err(HttpError::Unavailable("userinfo in URL is rejected".to_string()));
    }
    let host = if host_port.starts_with('[') {
        let end = host_port
            .find(']')
            .ok_or_else(|| HttpError::Unavailable("invalid IPv6 host".to_string()))?;
        &host_port[1..end]
    } else {
        match host_port.rsplit_once(':') {
            Some((h, _)) => h,
            None => host_port,
        }
    };
    let lower = host.to_ascii_lowercase();
    if lower != "127.0.0.1" && lower != "::1" && lower != "localhost" {
        return Err(HttpError::Unavailable(format!(
            "non-loopback host rejected: {host}"
        )));
    }
    Ok((host_port, path))
}

fn resolve_loopback_addrs(host_port: &str) -> Result<Vec<SocketAddr>, HttpError> {
    let addrs: Vec<SocketAddr> = host_port
        .to_socket_addrs()
        .map_err(|e| HttpError::Unavailable(format!("invalid host:port {host_port}: {e}")))?
        .collect();
    if addrs.is_empty() || addrs.iter().any(|a| !a.ip().is_loopback()) {
        return Err(HttpError::Unavailable(format!(
            "non-loopback address resolved for {host_port}"
        )));
    }
    Ok(addrs)
}

pub fn post_json(url: &str, body: &str, deadline: Duration) -> Result<(u16, JsonValue), HttpError> {
    let (host_port, path) = parse_loopback_host_port(url)?;
    let addrs = resolve_loopback_addrs(host_port)?;

    let start = Instant::now();
    let mut stream_opt = None;
    let mut last_err = None;
    for addr in addrs {
        let rem = deadline.saturating_sub(start.elapsed());
        if rem.is_zero() {
            return Err(HttpError::Timeout);
        }
        match TcpStream::connect_timeout(&addr, rem) {
            Ok(s) => {
                stream_opt = Some(s);
                break;
            }
            Err(e) if e.kind() == std::io::ErrorKind::TimedOut => return Err(HttpError::Timeout),
            Err(e) => last_err = Some(e.to_string()),
        }
    }
    let mut stream = stream_opt.ok_or_else(|| {
        HttpError::Unavailable(last_err.unwrap_or_else(|| "connection failed".to_string()))
    })?;

    let rem = deadline.saturating_sub(start.elapsed());
    if rem.is_zero() {
        return Err(HttpError::Timeout);
    }
    let _ = stream.set_write_timeout(Some(rem));

    let req = format!(
        "POST {path} HTTP/1.1\r\nHost: {host_port}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(req.as_bytes()).map_err(|e| {
        if e.kind() == std::io::ErrorKind::TimedOut || e.kind() == std::io::ErrorKind::WouldBlock {
            HttpError::Timeout
        } else {
            HttpError::Unavailable(e.to_string())
        }
    })?;

    let mut raw = Vec::new();
    let mut buf = [0u8; 8192];
    loop {
        let rem = deadline.saturating_sub(start.elapsed());
        if rem.is_zero() {
            return Err(HttpError::Timeout);
        }
        let _ = stream.set_read_timeout(Some(rem));
        match stream.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => {
                raw.extend_from_slice(&buf[..n]);
                if raw.len() > 64 * 1024 * 1024 {
                    return Err(HttpError::InvalidShape);
                }
            }
            Err(e)
                if e.kind() == std::io::ErrorKind::TimedOut
                    || e.kind() == std::io::ErrorKind::WouldBlock =>
            {
                return Err(HttpError::Timeout);
            }
            Err(e) => return Err(HttpError::Unavailable(e.to_string())),
        }
    }

    let sep = raw
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .ok_or(HttpError::InvalidJson)?;
    let header_str = std::str::from_utf8(&raw[..sep]).map_err(|_| HttpError::InvalidJson)?;
    let mut lines = header_str.split("\r\n");
    let status_line = lines.next().ok_or(HttpError::InvalidJson)?;
    let status: u16 = status_line
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse().ok())
        .ok_or(HttpError::InvalidJson)?;

    let is_chunked = lines.any(|l| {
        let lower = l.to_ascii_lowercase();
        lower.starts_with("transfer-encoding:") && lower.contains("chunked")
    });

    let body_bytes = if is_chunked {
        decode_chunked(&raw[sep + 4..]).ok_or(HttpError::InvalidJson)?
    } else {
        raw[sep + 4..].to_vec()
    };

    let body_str = std::str::from_utf8(&body_bytes).map_err(|_| HttpError::InvalidJson)?;
    let val = parse(body_str).map_err(|_| HttpError::InvalidJson)?;
    if val.as_object().is_none() {
        return Err(HttpError::InvalidShape);
    }
    if status != 200 && val.get("error").is_none() {
        return Err(HttpError::HttpStatus(status));
    }
    Ok((status, val))
}

fn decode_chunked(mut data: &[u8]) -> Option<Vec<u8>> {
    let mut out = Vec::new();
    loop {
        let pos = data.windows(2).position(|w| w == b"\r\n")?;
        let size_line = std::str::from_utf8(&data[..pos]).ok()?;
        let hex = size_line.split(';').next()?.trim();
        let size = usize::from_str_radix(hex, 16).ok()?;
        data = &data[pos + 2..];
        if size == 0 {
            return Some(out);
        }
        if data.len() < size + 2 {
            return None;
        }
        out.extend_from_slice(&data[..size]);
        data = &data[size + 2..];
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;
    use std::thread;

    #[test]
    fn test_rejects_non_loopback_and_userinfo() {
        assert!(matches!(
            post_json("http://example.com:8765/route_task", "{}", Duration::from_secs(1)),
            Err(HttpError::Unavailable(_))
        ));
        assert!(matches!(
            post_json("http://192.168.1.10:8765/route_task", "{}", Duration::from_secs(1)),
            Err(HttpError::Unavailable(_))
        ));
        assert!(matches!(
            post_json("http://user@127.0.0.1:8765/route_task", "{}", Duration::from_secs(1)),
            Err(HttpError::Unavailable(_))
        ));
    }

    #[test]
    fn test_localhost_and_per_read_deadline() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let srv = thread::spawn(move || {
            let (mut s, _) = listener.accept().unwrap();
            let mut buf = [0u8; 512];
            let _ = s.read(&mut buf);
            thread::sleep(Duration::from_millis(250));
            let _ = s.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}");
        });
        let res = post_json(&format!("http://localhost:{port}/route_task"), "{}", Duration::from_millis(60));
        assert_eq!(res, Err(HttpError::Timeout));
        let _ = srv.join();
    }
}
