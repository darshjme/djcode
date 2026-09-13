//! DJcode's local DAF scheduler. Tool requests/results cross real loopback DDAL frames.
use anyhow::{Context, Result, bail};
use daf_ddal::{DdalCodec, Frame, FrameType};
use daf_graph::{
    Edge, EdgeKind, ExecutionGraph, GraphExecutor, Node, NodeKind, TaskHandler, TaskSpec,
};
use futures::{SinkExt, StreamExt};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    collections::{HashMap, HashSet},
    sync::Arc,
    time::Duration,
};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    net::{TcpListener, TcpStream},
    sync::{Mutex, oneshot},
};
use tokio_util::codec::Framed;

#[derive(Clone, Deserialize)]
struct Work {
    id: String,
    name: String,
    arguments: Value,
    #[serde(default)]
    dependencies: Vec<String>,
}
#[derive(Deserialize)]
struct Batch {
    nodes: Vec<Work>,
    #[serde(default = "one")]
    concurrency: usize,
}
fn one() -> usize {
    1
}
type Pending = Arc<Mutex<HashMap<String, oneshot::Sender<Value>>>>;
async fn emit(value: Value) -> Result<()> {
    let mut out = tokio::io::stdout();
    let mut data = serde_json::to_vec(&value)?;
    data.push(b'\n');
    out.write_all(&data).await?;
    out.flush().await?;
    Ok(())
}
#[tokio::main]
async fn main() -> Result<()> {
    let mut lines = BufReader::new(tokio::io::stdin()).lines();
    let batch: Batch = serde_json::from_str(&lines.next_line().await?.context("missing batch")?)?;
    let names: HashSet<_> = batch.nodes.iter().map(|n| n.id.clone()).collect();
    if batch.nodes.is_empty()
        || batch.nodes.len() > 32
        || names.len() != batch.nodes.len()
        || !(1..=4).contains(&batch.concurrency)
    {
        bail!("invalid batch limits or duplicate node IDs");
    }
    let mut graph = ExecutionGraph::new("djcode");
    let mut ids = HashMap::new();
    let mut work = HashMap::new();
    for item in &batch.nodes {
        let node = Node::new(NodeKind::Task, &item.id).with_task_spec(TaskSpec {
            task_type: "djcode_tool".into(),
            params: Value::Null,
            timeout: Some(Duration::from_secs(3600)),
            max_retries: 0,
        });
        let id = graph.add_node(node);
        ids.insert(item.id.clone(), id);
        work.insert(id, item.clone());
    }
    for item in &batch.nodes {
        for dep in &item.dependencies {
            graph.add_edge(Edge::new(
                *ids.get(dep).context("unknown dependency")?,
                ids[&item.id],
                EdgeKind::DependsOn,
            ))?;
        }
    }
    let pending: Pending = Arc::new(Mutex::new(HashMap::new()));
    let replies = pending.clone();
    let reader = tokio::spawn(async move {
        while let Ok(Some(line)) = lines.next_line().await {
            let Ok(value) = serde_json::from_str::<Value>(&line) else {
                break;
            };
            if let Some(id) = value["id"].as_str() {
                if let Some(sender) = replies.lock().await.remove(id) {
                    let _ = sender.send(value);
                }
            }
        }
        replies.lock().await.clear();
    });
    let output_lock = Arc::new(Mutex::new(()));
    let handler: TaskHandler = Arc::new(move |id| {
        let item = work[&id].clone();
        let pending = pending.clone();
        let output_lock = output_lock.clone();
        Box::pin(async move {
            let exchange = async {
                let listener = TcpListener::bind("127.0.0.1:0").await?;
                let addr = listener.local_addr()?;
                let request = json!({"event":"tool", "id":item.id, "name":item.name,"arguments":item.arguments});
                let payload = serde_json::to_vec(&request)?;
                let expected = payload.clone();
                let server = async {
                    let (socket, _) = listener.accept().await?;
                    let mut wire = Framed::new(socket, DdalCodec::new());
                    let frame = wire.next().await.context("request EOF")??;
                    if frame.stream_id != 1
                        || frame.frame_type != FrameType::Data
                        || frame.payload.as_ref() != expected
                    {
                        bail!("DDAL request mismatch");
                    }
                    let (sender, receiver) = oneshot::channel();
                    pending.lock().await.insert(item.id.clone(), sender);
                    {
                        let _guard = output_lock.lock().await;
                        emit(serde_json::from_slice(&frame.payload)?).await?;
                    }
                    let response = receiver.await.context("host disconnected")?;
                    wire.send(Frame::data(1, serde_json::to_vec(&response)?.into()))
                        .await?;
                    Ok::<_, anyhow::Error>(())
                };
                let client = async {
                    let mut wire = Framed::new(TcpStream::connect(addr).await?, DdalCodec::new());
                    let frame = Frame::data(1, payload.into());
                    let sent = frame.encode_to_bytes().len();
                    wire.send(frame).await?;
                    let frame = wire.next().await.context("response EOF")??;
                    if frame.stream_id != 1 || frame.frame_type != FrameType::Data {
                        bail!("DDAL response mismatch");
                    }
                    let response: Value = serde_json::from_slice(&frame.payload)?;
                    if response["id"] != item.id {
                        bail!("DDAL response ID mismatch");
                    }
                    let _guard = output_lock.lock().await;
                    emit(json!({"event":"wire", "id":item.id, "sent_bytes":sent,"received_bytes":frame.encode_to_bytes().len(),"ok":response["ok"]})).await?;
                    if response["ok"] != true {
                        bail!("tool failed");
                    }
                    Ok::<_, anyhow::Error>(())
                };
                tokio::try_join!(server, client)?;
                Ok::<_, anyhow::Error>(())
            };
            exchange.await.map_err(|e| e.to_string())
        })
    });
    let executor = GraphExecutor::new(graph, handler)
        .with_max_concurrency(batch.concurrency)
        .with_global_timeout(Duration::from_secs(3600));
    let result = executor.execute().await;
    reader.abort();
    let states: Vec<_> = executor
        .graph()
        .read()
        .nodes()
        .map(|n| json!({"id":n.name,"state":format!("{:?}",n.state)}))
        .collect();
    let ok = result.is_ok() && states.iter().all(|s| s["state"] == "Succeeded");
    emit(json!({"event":"complete","ok":ok,"states":states})).await?;
    Ok(())
}
