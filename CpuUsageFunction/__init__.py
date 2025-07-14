import azure.functions as func
import logging
import json
from azure.identity import DefaultAzureCredential
from azure.monitor.query import MetricsQueryClient
from datetime import datetime, timedelta
from azure.mgmt.compute import ComputeManagementClient

def main(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Analyzing VM CPU usage for start/stop recommendations.')

    try:
        credential = DefaultAzureCredential()
        subscription_id = "5d36b86e-695f-427b-9a19-7a6cc2db39d6"  # Replace this
        compute_client = ComputeManagementClient(credential, subscription_id)
        client = MetricsQueryClient(credential)

        vm_results = []

        for vm in compute_client.virtual_machines.list_all():
            vm_resource_id = vm.id
            end_time = datetime.utcnow()
            start_time = end_time - timedelta(days=30)
            response = client.query_resource(
                resource_uri=vm_resource_id,
                metric_names=["Percentage CPU"],
                timespan=(start_time, end_time),
                granularity=timedelta(minutes=15)
            )

            cpu_values = []
            for metric in response.metrics:
                for ts in metric.timeseries:
                    for point in ts.data:
                        if point.average is not None:
                            cpu_values.append({
                                "timestamp": point.timestamp.isoformat(),
                                "value": point.average
                            })

            from collections import defaultdict

            daily_data = defaultdict(list)
            for entry in cpu_values:
                date_key = entry["timestamp"][:10]  # extract YYYY-MM-DD
                daily_data[date_key].append(entry)

            daily_results = []

            for date, entries in daily_data.items():
                stop_time = None
                start_time = None
                is_vm_stopped = False
                window = 4
                values = [e["value"] for e in entries]
                timestamps = [e["timestamp"] for e in entries]

                for i in range(1, len(values) - window + 1):
                    prev_vals = values[i-1:i-1+window]
                    w_vals = values[i:i+window]
                    w_times = timestamps[i:i+window]
                    avg = sum(w_vals) / window
                    change = max(w_vals) - min(w_vals)

                    # Calculate deviation between current and previous window
                    deviation = max(abs(w - p) for w, p in zip(w_vals, prev_vals))

                    if not is_vm_stopped and all(v < 30 for v in w_vals) and change < 5:
                        stop_time = w_times[0]
                        is_vm_stopped = True
                        continue

                    if is_vm_stopped and (deviation > 5 or max(w_vals) > 70):
                        start_time = w_times[0]
                        break

                daily_results.append({
                    "date": date,
                    "stop_time": stop_time,
                    "start_time": start_time
                })

            vm_results.append({
                "name": vm.name,
                "cpu_values": cpu_values,
                "daily_recommendations": daily_results
            })

        html = "<html><body>"
        for vm_result in vm_results:
            html += f"<h2>{vm_result['name']} - CPU Data</h2><table border='1'><tr><th>Timestamp</th><th>Value</th></tr>"
            for item in vm_result["cpu_values"]:
                html += f"<tr><td>{item['timestamp']}</td><td>{item['value']}</td></tr>"
            html += "</table>"

            html += "<h3>Recommended Stop/Start Times (Daily)</h3><table border='1'><tr><th>Date</th><th>Type</th><th>Timestamp</th></tr>"
            if not vm_result["daily_recommendations"]:
                html += "<tr><td colspan='3'>No recommendations available for this VM</td></tr>"
            for rec in vm_result["daily_recommendations"]:
                if rec["stop_time"]:
                    html += f"<tr><td>{rec['date']}</td><td>STOP</td><td>{rec['stop_time']}</td></tr>"
                if rec["start_time"]:
                    html += f"<tr><td>{rec['date']}</td><td>START</td><td>{rec['start_time']}</td></tr>"
            html += "</table><br>"
        html += "</body></html>"

        return func.HttpResponse(
            html,
            mimetype="text/html",
            status_code=200
        )

    except Exception as e:
        logging.error(f"Error: {e}")
        return func.HttpResponse(
            f"Error: {str(e)}",
            status_code=500
        )