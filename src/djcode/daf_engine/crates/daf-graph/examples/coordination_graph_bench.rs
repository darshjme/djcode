//! Controlled GraphExecutor component probes; see outputs/graph-method.md.
use std::{sync::{Arc, Mutex}, time::{Duration, Instant}};
use daf_graph::{ExecutionGraph, GraphExecutor, Node, NodeKind, TaskSpec, TaskHandler, Edge, EdgeKind};
use serde_json::json;

#[derive(Default)]
struct State { outputs: [Option<i64>; 4], calls: [usize; 4] }

#[tokio::main(flavor = "current_thread")]
async fn main() {
    for trial in 0..30 {
        for condition in ["diamond", "fail_once", "node_timeout"] {
            let seed = trial as i64 + 11;
            let mut graph = ExecutionGraph::new(condition);
            let ids: Vec<_> = ["A", "B", "C", "D"].iter().enumerate().map(|(i, name)| {
                graph.add_node(Node::new(NodeKind::Task, *name).with_task_spec(TaskSpec {
                    task_type: "deterministic_arithmetic".into(), params: json!({"index": i}),
                    timeout: None, max_retries: if condition == "fail_once" && i == 1 { 1 } else { 0 },
                }))
            }).collect();
            for (from, to) in [(0,1), (0,2), (1,3), (2,3)] {
                graph.add_edge(Edge::new(ids[from], ids[to], EdgeKind::DependsOn)).unwrap();
            }
            let shared = Arc::new(Mutex::new(State::default()));
            let state = shared.clone();
            let handler: TaskHandler = Arc::new(move |id| {
                let i = ids.iter().position(|&n| n == id).unwrap();
                let state = state.clone();
                Box::pin(async move {
                    let call = { let mut s = state.lock().unwrap(); s.calls[i] += 1; s.calls[i] };
                    if i == 1 && condition == "fail_once" && call == 1 { return Err("controlled first-attempt failure".into()); }
                    if i == 1 && condition == "node_timeout" { tokio::time::sleep(Duration::from_millis(200)).await; }
                    if i == 1 || i == 2 { tokio::time::sleep(Duration::from_millis(2)).await; }
                    let mut s = state.lock().unwrap();
                    let value = match i {
                        0 => seed,
                        1 => s.outputs[0].ok_or("A input missing")? + 3,
                        2 => s.outputs[0].ok_or("A input missing")? * 2,
                        3 => s.outputs[1].ok_or("B input missing")? + s.outputs[2].ok_or("C input missing")?,
                        _ => unreachable!(),
                    };
                    s.outputs[i] = Some(value);
                    Ok(())
                })
            });
            let executor = GraphExecutor::new(graph, handler).with_max_concurrency(2)
                .with_node_timeout(Duration::from_millis(if condition == "node_timeout" {10} else {1000}));
            let started = Instant::now();
            let result = executor.execute().await;
            let elapsed_us = started.elapsed().as_micros();
            let s = shared.lock().unwrap();
            let expected = [Some(seed), Some(seed+3), Some(seed*2), Some(seed*3+3)];
            let correct = s.outputs == expected;
            let (completed, failed, skipped, error) = match result {
                Ok(p) => (p.nodes_completed, p.nodes_failed, p.nodes_skipped, None),
                Err(e) => (0, 0, 0, Some(e.to_string())),
            };
            let contained = failed == 1 && skipped == 1 && s.calls[3] == 0 && s.outputs[3].is_none()
                && s.outputs[2] == Some(seed*2);
            let pass = match condition {
                "diamond" => correct && s.calls == [1,1,1,1] && failed == 0 && skipped == 0,
                "fail_once" => correct && s.calls == [1,2,1,1] && failed == 0 && skipped == 0,
                "node_timeout" => contained && elapsed_us < 150_000,
                _ => unreachable!(),
            } && error.is_none();
            println!("{}", json!({"trial":trial,"condition":condition,"elapsed_us":elapsed_us,
                "outputs":s.outputs,"expected_outputs":expected,"calls":s.calls,"correct_final_output":correct,
                "terminal_nodes":completed,"failed_nodes":failed,"skipped_nodes":skipped,
                "failure_contained":contained,"condition_pass":pass,"execute_error":error}));
        }
    }
}
