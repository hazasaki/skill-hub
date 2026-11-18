//! Minimal RFC 8259 JSON parser and deterministic serializer (zero external crates).

use std::collections::BTreeMap;
use std::fmt;

#[derive(Debug, Clone, PartialEq)]
pub enum JsonValue {
    Null,
    Bool(bool),
    Number(JsonNumber),
    String(String),
    Array(Vec<JsonValue>),
    Object(BTreeMap<String, JsonValue>),
}

#[derive(Debug, Clone, PartialEq)]
pub struct JsonNumber {
    pub raw: String,
}

impl JsonNumber {
    pub fn as_f64(&self) -> Option<f64> {
        self.raw.parse::<f64>().ok()
    }
    pub fn as_i64(&self) -> Option<i64> {
        self.raw.parse::<i64>().ok()
    }
    pub fn as_u64(&self) -> Option<u64> {
        self.raw.parse::<u64>().ok()
    }
}

impl JsonValue {
    pub fn as_str(&self) -> Option<&str> {
        match self {
            JsonValue::String(s) => Some(s.as_str()),
            _ => None,
        }
    }
    pub fn as_bool(&self) -> Option<bool> {
        match self {
            JsonValue::Bool(b) => Some(*b),
            _ => None,
        }
    }
    pub fn as_array(&self) -> Option<&[JsonValue]> {
        match self {
            JsonValue::Array(a) => Some(a.as_slice()),
            _ => None,
        }
    }
    pub fn as_object(&self) -> Option<&BTreeMap<String, JsonValue>> {
        match self {
            JsonValue::Object(o) => Some(o),
            _ => None,
        }
    }
    pub fn as_object_mut(&mut self) -> Option<&mut BTreeMap<String, JsonValue>> {
        match self {
            JsonValue::Object(o) => Some(o),
            _ => None,
        }
    }
    pub fn get(&self, key: &str) -> Option<&JsonValue> {
        self.as_object().and_then(|o| o.get(key))
    }
    pub fn is_null(&self) -> bool {
        matches!(self, JsonValue::Null)
    }
    pub fn as_f64(&self) -> Option<f64> {
        match self {
            JsonValue::Number(n) => n.as_f64(),
            _ => None,
        }
    }
    pub fn from_f64(v: f64) -> Self {
        if v.is_finite() {
            let mut s = format!("{v}");
            if s == "-0" {
                s = "0".to_string();
            }
            JsonValue::Number(JsonNumber { raw: s })
        } else {
            JsonValue::Null
        }
    }
    pub fn from_u64(v: u64) -> Self {
        JsonValue::Number(JsonNumber { raw: v.to_string() })
    }
}

#[derive(Debug, Clone)]
pub struct ParseError {
    pub message: String,
    pub pos: usize,
}

impl fmt::Display for ParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{} at byte {}", self.message, self.pos)
    }
}

impl std::error::Error for ParseError {}

struct Parser<'a> {
    bytes: &'a [u8],
    pos: usize,
}

impl<'a> Parser<'a> {
    fn new(input: &'a str) -> Self {
        let input = input.strip_prefix('\u{feff}').unwrap_or(input);
        Self {
            bytes: input.as_bytes(),
            pos: 0,
        }
    }

    fn err(&self, msg: impl Into<String>) -> ParseError {
        ParseError {
            message: msg.into(),
            pos: self.pos,
        }
    }

    fn skip_ws(&mut self) {
        while self.pos < self.bytes.len() {
            match self.bytes[self.pos] {
                b' ' | b'\t' | b'\r' | b'\n' => self.pos += 1,
                _ => break,
            }
        }
    }

    fn peek(&self) -> Option<u8> {
        self.bytes.get(self.pos).copied()
    }

    fn parse_value(&mut self, depth: usize) -> Result<JsonValue, ParseError> {
        if depth > 128 {
            return Err(self.err("JSON nesting depth exceeded"));
        }
        self.skip_ws();
        let b = self.peek().ok_or_else(|| self.err("unexpected EOF"))?;
        match b {
            b'n' => {
                if self.bytes[self.pos..].starts_with(b"null") {
                    self.pos += 4;
                    Ok(JsonValue::Null)
                } else {
                    Err(self.err("invalid literal"))
                }
            }
            b't' => {
                if self.bytes[self.pos..].starts_with(b"true") {
                    self.pos += 4;
                    Ok(JsonValue::Bool(true))
                } else {
                    Err(self.err("invalid literal"))
                }
            }
            b'f' => {
                if self.bytes[self.pos..].starts_with(b"false") {
                    self.pos += 5;
                    Ok(JsonValue::Bool(false))
                } else {
                    Err(self.err("invalid literal"))
                }
            }
            b'"' => self.parse_string().map(JsonValue::String),
            b'[' => self.parse_array(depth + 1),
            b'{' => self.parse_object(depth + 1),
            b'-' | b'0'..=b'9' => self.parse_number(),
            _ => Err(self.err("unexpected character")),
        }
    }

    fn parse_string(&mut self) -> Result<String, ParseError> {
        if self.peek() != Some(b'"') {
            return Err(self.err("expected '\"'"));
        }
        self.pos += 1;
        let mut out = String::new();
        while self.pos < self.bytes.len() {
            let b = self.bytes[self.pos];
            if b == b'"' {
                self.pos += 1;
                return Ok(out);
            }
            if b == b'\\' {
                self.pos += 1;
                let esc = self.peek().ok_or_else(|| self.err("unterminated escape"))?;
                self.pos += 1;
                match esc {
                    b'"' => out.push('"'),
                    b'\\' => out.push('\\'),
                    b'/' => out.push('/'),
                    b'b' => out.push('\u{0008}'),
                    b'f' => out.push('\u{000c}'),
                    b'n' => out.push('\n'),
                    b'r' => out.push('\r'),
                    b't' => out.push('\t'),
                    b'u' => {
                        let code = self.parse_hex4()?;
                        if (0xD800..=0xDBFF).contains(&code) {
                            if self.bytes.get(self.pos..self.pos + 2) == Some(b"\\u") {
                                self.pos += 2;
                                let low = self.parse_hex4()?;
                                if (0xDC00..=0xDFFF).contains(&low) {
                                    let cp = 0x10000
                                        + (((code as u32 - 0xD800) << 10) | (low as u32 - 0xDC00));
                                    let ch = char::from_u32(cp)
                                        .ok_or_else(|| self.err("invalid unicode scalar"))?;
                                    out.push(ch);
                                } else {
                                    return Err(self.err("invalid low surrogate"));
                                }
                            } else {
                                return Err(self.err("unpaired high surrogate"));
                            }
                        } else if (0xDC00..=0xDFFF).contains(&code) {
                            return Err(self.err("unpaired low surrogate"));
                        } else {
                            let ch = char::from_u32(code as u32)
                                .ok_or_else(|| self.err("invalid unicode scalar"))?;
                            out.push(ch);
                        }
                    }
                    _ => return Err(self.err("invalid escape")),
                }
            } else if b < 0x20 {
                return Err(self.err("unescaped control character"));
            } else {
                let s = std::str::from_utf8(&self.bytes[self.pos..])
                    .map_err(|_| self.err("invalid UTF-8"))?;
                let ch = s.chars().next().ok_or_else(|| self.err("unexpected EOF"))?;
                out.push(ch);
                self.pos += ch.len_utf8();
            }
        }
        Err(self.err("unterminated string"))
    }

    fn parse_hex4(&mut self) -> Result<u16, ParseError> {
        if self.pos + 4 > self.bytes.len() {
            return Err(self.err("short \\u escape"));
        }
        let hex = std::str::from_utf8(&self.bytes[self.pos..self.pos + 4])
            .map_err(|_| self.err("invalid \\u escape"))?;
        let val = u16::from_str_radix(hex, 16).map_err(|_| self.err("invalid hex in \\u"))?;
        self.pos += 4;
        Ok(val)
    }

    fn parse_number(&mut self) -> Result<JsonValue, ParseError> {
        let start = self.pos;
        if self.peek() == Some(b'-') {
            self.pos += 1;
        }
        match self.peek() {
            Some(b'0') => self.pos += 1,
            Some(b'1'..=b'9') => {
                self.pos += 1;
                while matches!(self.peek(), Some(b'0'..=b'9')) {
                    self.pos += 1;
                }
            }
            _ => return Err(self.err("invalid number")),
        }
        if self.peek() == Some(b'.') {
            self.pos += 1;
            if !matches!(self.peek(), Some(b'0'..=b'9')) {
                return Err(self.err("invalid fraction"));
            }
            while matches!(self.peek(), Some(b'0'..=b'9')) {
                self.pos += 1;
            }
        }
        if matches!(self.peek(), Some(b'e' | b'E')) {
            self.pos += 1;
            if matches!(self.peek(), Some(b'+' | b'-')) {
                self.pos += 1;
            }
            if !matches!(self.peek(), Some(b'0'..=b'9')) {
                return Err(self.err("invalid exponent"));
            }
            while matches!(self.peek(), Some(b'0'..=b'9')) {
                self.pos += 1;
            }
        }
        let raw = std::str::from_utf8(&self.bytes[start..self.pos])
            .map_err(|_| self.err("invalid utf8"))?
            .to_string();
        Ok(JsonValue::Number(JsonNumber { raw }))
    }

    fn parse_array(&mut self, depth: usize) -> Result<JsonValue, ParseError> {
        self.pos += 1;
        let mut items = Vec::new();
        self.skip_ws();
        if self.peek() == Some(b']') {
            self.pos += 1;
            return Ok(JsonValue::Array(items));
        }
        loop {
            items.push(self.parse_value(depth)?);
            self.skip_ws();
            match self.peek() {
                Some(b',') => self.pos += 1,
                Some(b']') => {
                    self.pos += 1;
                    return Ok(JsonValue::Array(items));
                }
                _ => return Err(self.err("expected ',' or ']'")),
            }
        }
    }

    fn parse_object(&mut self, depth: usize) -> Result<JsonValue, ParseError> {
        self.pos += 1;
        let mut map = BTreeMap::new();
        self.skip_ws();
        if self.peek() == Some(b'}') {
            self.pos += 1;
            return Ok(JsonValue::Object(map));
        }
        loop {
            self.skip_ws();
            let key = self.parse_string()?;
            self.skip_ws();
            if self.peek() != Some(b':') {
                return Err(self.err("expected ':'"));
            }
            self.pos += 1;
            let val = self.parse_value(depth)?;
            map.insert(key, val);
            self.skip_ws();
            match self.peek() {
                Some(b',') => self.pos += 1,
                Some(b'}') => {
                    self.pos += 1;
                    return Ok(JsonValue::Object(map));
                }
                _ => return Err(self.err("expected ',' or '}'")),
            }
        }
    }
}

pub fn parse(input: &str) -> Result<JsonValue, ParseError> {
    let mut p = Parser::new(input);
    let val = p.parse_value(0)?;
    p.skip_ws();
    if p.pos != p.bytes.len() {
        return Err(p.err("trailing data after JSON value"));
    }
    Ok(val)
}

pub fn to_string_compact(val: &JsonValue, ensure_ascii: bool) -> String {
    let mut out = String::new();
    write_compact(val, ensure_ascii, &mut out);
    out
}

fn write_compact(val: &JsonValue, ensure_ascii: bool, out: &mut String) {
    match val {
        JsonValue::Null => out.push_str("null"),
        JsonValue::Bool(true) => out.push_str("true"),
        JsonValue::Bool(false) => out.push_str("false"),
        JsonValue::Number(n) => out.push_str(&n.raw),
        JsonValue::String(s) => write_string(s, ensure_ascii, out),
        JsonValue::Array(arr) => {
            out.push('[');
            for (i, item) in arr.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_compact(item, ensure_ascii, out);
            }
            out.push(']');
        }
        JsonValue::Object(map) => {
            out.push('{');
            for (i, (k, v)) in map.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_string(k, ensure_ascii, out);
                out.push(':');
                write_compact(v, ensure_ascii, out);
            }
            out.push('}');
        }
    }
}

fn write_string(s: &str, ensure_ascii: bool, out: &mut String) {
    out.push('"');
    for ch in s.chars() {
        match ch {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\u{0008}' => out.push_str("\\b"),
            '\u{000c}' => out.push_str("\\f"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c if ensure_ascii && (c as u32) > 0x7f => {
                let code = c as u32;
                if code <= 0xFFFF {
                    out.push_str(&format!("\\u{:04x}", code));
                } else {
                    let adj = code - 0x10000;
                    let hi = 0xD800 + ((adj >> 10) & 0x3FF);
                    let lo = 0xDC00 + (adj & 0x3FF);
                    out.push_str(&format!("\\u{:04x}\\u{:04x}", hi, lo));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}
