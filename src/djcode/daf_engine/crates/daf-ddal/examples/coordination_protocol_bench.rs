use bytes::{Bytes,BytesMut};
use daf_ddal::{serialize_payload,deserialize_payload,PayloadFormat,Frame,DdalCodec,RoutingTable,RouteEntry};
use daf_core::AgentId;
use serde::{Serialize,Deserialize};
use tokio_util::codec::{Encoder,Decoder};
use std::{time::Instant,hint::black_box};
#[derive(Debug,Clone,Serialize,Deserialize,PartialEq)]
struct Record {task:u64,conversation:String,sequence:u32,context:String}
fn roundtrip(r:&Record,f:PayloadFormat)->usize {
 let p=serialize_payload(r,f).unwrap(); let frame=Frame::data(7,p); let mut b=BytesMut::new(); let mut c=DdalCodec::new();
 c.encode(frame,&mut b).unwrap(); let bytes=b.len(); let decoded=c.decode(&mut b).unwrap().unwrap();
 assert_eq!(decoded.stream_id,7); assert!(b.is_empty()); let back:Record=deserialize_payload(&decoded.payload,f).unwrap();assert_eq!(&back,r);black_box(back);bytes
}
fn main(){
 let fs=[PayloadFormat::Json,PayloadFormat::MessagePack,PayloadFormat::Bincode];
 for size in [256,4096,65536] {
 let r=Record{task:42,conversation:"fixed-conversation-001".into(),sequence:9,context:"requirement=keep exact context; ".repeat(size/31+1)[..size].into()};
 for f in fs {for _ in 0..10 {roundtrip(&r,f);}}
 for trial in 0..30 {for offset in 0..3 {let f=fs[(trial+offset)%3];let start=Instant::now();let mut bytes=0;for _ in 0..100 {bytes=roundtrip(&r,f);}println!("{}",serde_json::json!({"suite":"codec","trial":trial,"context_bytes":size,"format":f.as_str(),"iterations":100,"ns_per_op":start.elapsed().as_nanos() as f64/100.,"wire_bytes":bytes,"exact_pass":100}));}}
 }
 for trial in 0..30 {
 let payload=Bytes::from(format!("exact-context-{trial}"));let f=Frame::data(trial+1,payload.clone());let enc=f.encode_to_bytes();
 let start=Instant::now();let mut c=DdalCodec::new();let mut b=BytesMut::from(&enc[..10]);let partial=c.decode(&mut b).unwrap().is_none();b.extend_from_slice(&enc[10..]);let recovered=c.decode(&mut b).unwrap().unwrap();assert_eq!(recovered.payload,payload);
 let mut garbage=BytesMut::from(&b"garbage"[..]);garbage.extend_from_slice(&enc);let g=c.decode(&mut garbage).unwrap().unwrap();assert_eq!(g.payload,payload);
 let mut corrupt=enc.clone();let last=corrupt.len()-1;corrupt[last]^=1;corrupt.extend_from_slice(&enc);let rejected=c.decode(&mut corrupt).is_err();let next=c.decode(&mut corrupt).unwrap().unwrap();assert_eq!(next.payload,payload);
 assert!(partial&&rejected);println!("{}",serde_json::json!({"suite":"codec_faults","trial":trial,"partial_pass":partial,"garbage_pass":true,"corruption_rejected":rejected,"manual_next_decode_pass":true,"elapsed_ns":start.elapsed().as_nanos() as u64}));
 let table=RoutingTable::new();let ids:Vec<_>=(0..100).map(|_|AgentId::new()).collect();for (i,id) in ids.iter().enumerate(){table.insert(RouteEntry::new(*id,i as u32).with_priority(1));table.insert(RouteEntry::new(*id,1000+i as u32).with_priority(2));}
 let start=Instant::now();let mut exact=0;for (i,id) in ids.iter().enumerate(){assert_eq!(table.best_route(id).unwrap().channel_id,i as u32);exact+=1;assert!(table.remove_route(id,i as u32));assert_eq!(table.best_route(id).unwrap().channel_id,1000+i as u32);table.remove_agent(id);assert!(table.best_route(id).is_none());}
 println!("{}",serde_json::json!({"suite":"routing","trial":trial,"exact_routes":exact,"backup_pass":100,"missing_pass":100,"elapsed_ns":start.elapsed().as_nanos() as u64}));
 }
}
