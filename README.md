# Azure Function: VM CPU Start/Stop Advisor
import azure.functions as func
import logging

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

@app.route(route="CpuUsageFunction")
def CpuUsageFunction(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Python HTTP trigger function processed a request.')

    name = req.params.get('name')
    if not name:
        try:
            req_body = req.get_json()
        except ValueError:
            pass
        else:
            name = req_body.get('name')

    if name:
        return func.HttpResponse(f"Hello, {name}. This HTTP triggered function executed successfully.")
    else:
        return func.HttpResponse(
             "This HTTP triggered function executed successfully. Pass a name in the query string or in the request body for a personalized response.",
             status_code=200
        )

@app.route(route="http_trigger1", auth_level=func.AuthLevel.ANONYMOUS)
def http_trigger1(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Python HTTP trigger function processed a request.')

    name = req.params.get('name')
    if not name:
        try:
            req_body = req.get_json()
        except ValueError:
            pass
        else:
            name = req_body.get('name')

    if name:
        return func.HttpResponse(f"Hello, {name}. This HTTP triggered function executed successfully.")
    else:
        return func.HttpResponse(
             "This HTTP triggered function executed successfully. Pass a name in the query string or in the request body for a personalized response.",
             status_code=200
        )
-----------





import azure.functions as func
import logging
import json
from datetime import datetime, timedelta
from collections import defaultdict
from azure.identity import DefaultAzureCredential
from azure.monitor.query import MetricsQueryClient
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.resource import SubscriptionClient

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

@app.route(route="CpuUsageFunction")
def CpuUsageFunction(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Analyzing VM CPU usage for start/stop recommendations.')

    try:
        credential = DefaultAzureCredential()
        sub_client = SubscriptionClient(credential)
        client = MetricsQueryClient(credential)

        subscription = next(sub_client.subscriptions.list())
        subscription_id = subscription.subscription_id
        compute_client = ComputeManagementClient(credential, subscription_id)

        vm_results = []

        for vm in compute_client.virtual_machines.list_all():
            if "aks" in vm.name.lower() or "databricks" in vm.name.lower():
                continue
            
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

            daily_data = defaultdict(list)
            for entry in cpu_values:
                date_key = entry["timestamp"][:10]
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

        html = """
<html>
<head>
    <style>
        body { font-family: Arial, sans-serif; padding: 20px; background-color: #f5f5f5; }
        table { border-collapse: collapse; width: 100%; margin-bottom: 20px; background-color: #fff; }
        th, td { border: 1px solid #ccc; padding: 8px; text-align: left; }
        th { background-color: #f0f0f0; }
        h2, h3 { color: #333; }
    </style>
</head>
<body>
"""
        for vm_result in vm_results:
            html += f"""
  <table>
      <tr><th>VM Name</th></tr>
      <tr><td>{vm_result['name']}</td></tr>
  </table><br>
  """
            html += "<h3>Recommended Stop/Start Times (Daily)</h3><table><tr><th>Date</th><th>Type</th><th>Timestamp</th></tr>"
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
------------
import azure.functions as func
import logging
import json
from azure.identity import AzureCliCredential
from azure.monitor.query import MetricsQueryClient
from datetime import datetime, timedelta
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.resource import SubscriptionClient
import calendar
from statistics import median


IDLE_HOURS = 4  # increase idle window to 4 hours for better shutdown decisions
WINDOW_SIZE = int((IDLE_HOURS * 60) / 15)  # 4 hours = 16 samples for 15-min granularity

def main(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Analyzing VM CPU usage for start/stop recommendations.')

    try:
        credential = AzureCliCredential()
        sub_client = SubscriptionClient(credential)
        client = MetricsQueryClient(credential)

        # Skip management groups and get current subscription ID
        subscription = next(sub_client.subscriptions.list())
        subscription_id = "5d36b86e-695f-427b-9a19-7a6cc2db39d6"
        compute_client = ComputeManagementClient(credential, subscription_id)

        vm_results = []

        # List all VMs in the subscription and filter AKS/Databricks
        for vm in compute_client.virtual_machines.list_all():
            if "aks" in vm.name.lower() or "databricks" in vm.name.lower():
                continue
            
            vm_resource_id = vm.id
            end_time = datetime.utcnow()
            start_time = end_time - timedelta(days=10)
            response = client.query_resource(
                resource_uri=vm_resource_id,
                metric_names=[
                    "Percentage CPU",
                    "OS Disk Read Bytes/sec",
                    "OS Disk Write Bytes/sec"
                ],
                timespan=(start_time, end_time),
                granularity=timedelta(minutes=15)
            )

            cpu_values, disk_values = [], []
            timestamps = []
            disk_reads, disk_writes = {}, {}

            for metric in response.metrics:
                if metric.name == "Percentage CPU":
                    for ts in metric.timeseries:
                        for point in ts.data:
                            if point.average is not None:
                                timestamps.append(point.timestamp.isoformat())
                                cpu_values.append(point.average)
                elif metric.name == "OS Disk Read Bytes/sec":
                    for ts in metric.timeseries:
                        for point in ts.data:
                            if point.average is not None:
                                disk_reads[point.timestamp.isoformat()] = point.average
                elif metric.name == "OS Disk Write Bytes/sec":
                    for ts in metric.timeseries:
                        for point in ts.data:
                            if point.average is not None:
                                disk_writes[point.timestamp.isoformat()] = point.average

            # Combine disk read + write for same timestamp
            for ts in timestamps:
                read_val = disk_reads.get(ts, 0)
                write_val = disk_writes.get(ts, 0)
                disk_values.append(read_val + write_val)

            def calculate_dynamic_thresholds(values):
                sorted_vals = sorted(values)
                n = len(sorted_vals)
                if n == 0:
                    return 0, 0  # avoid division by zero
                
                q1_idx = int(0.25 * (n + 1))
                q3_idx = int(0.75 * (n + 1))
                q1 = sorted_vals[max(0, q1_idx - 1)]
                q3 = sorted_vals[min(n - 1, q3_idx - 1)]
                iqr = q3 - q1
                
                # Dynamic thresholds
                dynamic_lower = max(0, q1 - 1.5 * iqr)
                dynamic_upper = q3 + 1.5 * iqr
                
                # ✅ Enforce minimum idle threshold of 10%
                fixed_min_idle = 10.0
                final_idle_threshold = min(dynamic_lower, fixed_min_idle)
                
                return final_idle_threshold, dynamic_upper

            def detect_idle_windows(cpu_vals, disk_vals, ts_vals):
                idle_windows = []
                busy_windows = []

                cpu_idle_threshold, cpu_busy_threshold = calculate_dynamic_thresholds(cpu_vals)
                disk_idle_threshold, disk_busy_threshold = calculate_dynamic_thresholds(disk_vals)

                for i in range(len(cpu_vals) - WINDOW_SIZE + 1):
                    cpu_window = cpu_vals[i:i+WINDOW_SIZE]
                    disk_window = disk_vals[i:i+WINDOW_SIZE]
                    time_window = ts_vals[i:i+WINDOW_SIZE]

                    if all(cpu < cpu_idle_threshold for cpu in cpu_window) and \
                       all(disk < disk_idle_threshold for disk in disk_window):
                        idle_windows.append({
                            "start": time_window[0],
                            "end": time_window[-1],
                            "avg_cpu": sum(cpu_window)/len(cpu_window),
                            "avg_disk": sum(disk_window)/len(disk_window)
                        })

                    if max(cpu_window) > cpu_busy_threshold or \
                       max(disk_window) > disk_busy_threshold:
                        busy_windows.append(time_window[0])

                if not busy_windows:
                    busy_windows = ["No busy workload detected"]

                return idle_windows, busy_windows

            from collections import defaultdict
            daily_data = defaultdict(list)
            for i, cpu_val in enumerate(cpu_values):
                date_key = timestamps[i][:10]
                date_obj = datetime.strptime(date_key, "%Y-%m-%d")
                if date_obj.weekday() < 5:  # Only add weekdays
                    daily_data[date_key].append({
                        "timestamp": timestamps[i],
                        "value": cpu_val,
                        "disk": disk_values[i] if i < len(disk_values) else 0
                    })

            daily_results = []
            sorted_dates = sorted(daily_data.keys())
            i = 0
            while i < len(sorted_dates):
                date = sorted_dates[i]
                date_obj = datetime.strptime(date, "%Y-%m-%d")
                if date_obj.weekday() >= 5:
                    i += 1
                    continue

                entries = daily_data[date]
                values = [e["value"] for e in entries]
                timestamps_day = [e["timestamp"] for e in entries]
                disk_vals = [e["disk"] for e in entries]

                day_min_cpu = min(values) if values else None
                day_max_cpu = max(values) if values else None
                day_avg_cpu = sum(values)/len(values) if values else None

                idle_windows, busy_windows = detect_idle_windows(values, disk_vals, timestamps_day)

                stop_time = idle_windows[0]["start"] if idle_windows else "No sustained idle window detected"
                start_time = busy_windows[0] if busy_windows else "No busy workload detected"

                stop_window_cpu_usage = idle_windows[0]["avg_cpu"] if idle_windows else "No sustained idle usage"
                start_window_cpu_usage = None
                start_window_disk_usage = None
                stop_window_disk_usage = idle_windows[0]["avg_disk"] if idle_windows else "No sustained idle usage"
                if start_time and start_time != "No busy workload detected":
                    try:
                        idx = timestamps_day.index(start_time)
                        start_slice = values[idx:idx+WINDOW_SIZE]
                        start_disk_slice = disk_vals[idx:idx+WINDOW_SIZE]
                        start_window_cpu_usage = sum(start_slice)/len(start_slice) if start_slice else None
                        start_window_disk_usage = sum(start_disk_slice)/len(start_disk_slice) if start_disk_slice else None
                    except:
                        start_window_cpu_usage = None
                        start_window_disk_usage = None

                decision = "Safe to stop VM " if idle_windows else "Monitor  moderate usage"

                daily_results.append({
                    "date": date,
                    "day": date_obj.strftime("%A"),
                    "week": f"Week {(date_obj.day - 1) // 7 + 1} of {calendar.month_name[date_obj.month]}",
                    "stop_time": stop_time,
                    "start_time": start_time,
                    "stop_window_cpu_usage": stop_window_cpu_usage,
                    "start_window_cpu_usage": start_window_cpu_usage,
                    "stop_window_disk_usage": stop_window_disk_usage,
                    "start_window_disk_usage": start_window_disk_usage,
                    "decision": decision
                })
                i += 1

            vm_results.append({
                "name": vm.name,
                "daily_recommendations": daily_results
            })

        return func.HttpResponse(
            json.dumps(vm_results, indent=2),
            mimetype="application/json",
            status_code=200
        )

    except Exception as e:
        logging.error(f"Error: {e}")
        return func.HttpResponse(
            f"Error: {str(e)}",
            status_code=500
        )
--------------


{
  "policyRule": {
    "if": {
      "allOf": [
        {
          "field": "type",
          "equals": "Microsoft.Compute/disks"
        },
        {
          "anyOf": [
            {
              "field": "Microsoft.Compute/disks/sku.name",
              "in": [
                "Premium_LRS",
                "Premium_ZRS",
                "PremiumV2_LRS"
              ]
            },
            {
              "field": "Microsoft.Compute/disks/sku.tier",
              "equals": "Premium"
            }
          ]
        }
      ]
    },
    "then": {
      "effect": "deny"
    }
  }
}
