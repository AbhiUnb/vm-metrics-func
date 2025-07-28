# Azure Function: VM CPU Start/Stop Advisor
---------------------------------------------
import azure.functions as func
import logging
import json
from azure.monitor.query import MetricsQueryClient
from datetime import datetime, timedelta
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.resource import SubscriptionClient
import calendar
from statistics import median

# Added for Managed Identity and Management Groups
from azure.identity import ManagedIdentityCredential
from azure.mgmt.managementgroups import ManagementGroupsAPI


IDLE_HOURS = 4  # increase idle window to 4 hours for better shutdown decisions
WINDOW_SIZE = int((IDLE_HOURS * 60) / 15)  # 4 hours = 16 samples for 15-min granularity

def main(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Analyzing VM CPU usage for start/stop recommendations.')

    try:
        # Authenticate using User Assigned Managed Identity
        USER_ASSIGNED_CLIENT_ID = "YOUR-USER-ASSIGNED-MSI-CLIENT-ID"
        credential = ManagedIdentityCredential(client_id=USER_ASSIGNED_CLIENT_ID)

        client = MetricsQueryClient(credential)

        # Prepare Management Groups API client
        mgmt_client = ManagementGroupsAPI(credential)

        # Collect all subscription IDs from all management groups
        all_subscriptions = []
        for mg in mgmt_client.management_groups.list():
            logging.info(f"Found Management Group: {mg.name}")
            entities = mgmt_client.entities.list(group_name=mg.name)
            for entity in entities:
                if entity.type.lower() == "subscriptions":
                    logging.info(f"  ↳ Subscription: {entity.name}")
                    all_subscriptions.append(entity.name)

        vm_results = []

        # Iterate over all subscriptions and analyze VMs
        for subscription_id in all_subscriptions:
            compute_client = ComputeManagementClient(credential, subscription_id)
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
                # Adaptive idle threshold: 60% of daily average but never below 5%
                daily_avg = sum(values) / len(values) if values else 0
                adaptive_idle = max(5.0, daily_avg * 0.6)
                return adaptive_idle, dynamic_upper

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

                if not idle_windows:
                    # Pick the window with the lowest avg CPU+disk usage as fallback
                    min_avg = float("inf")
                    fallback_window = None
                    for i in range(len(cpu_vals) - WINDOW_SIZE + 1):
                        cpu_window = cpu_vals[i:i+WINDOW_SIZE]
                        disk_window = disk_vals[i:i+WINDOW_SIZE]
                        avg_combined = (sum(cpu_window) / len(cpu_window)) + (sum(disk_window) / len(disk_window))
                        if avg_combined < min_avg:
                            min_avg = avg_combined
                            fallback_window = {
                                "start": ts_vals[i],
                                "end": ts_vals[i + WINDOW_SIZE - 1],
                                "avg_cpu": sum(cpu_window) / len(cpu_window),
                                "avg_disk": sum(disk_window) / len(disk_window)
                            }
                    if fallback_window:
                        idle_windows.append(fallback_window)

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

                stop_window_cpu_usage = idle_windows[0]["avg_cpu"] if idle_windows else None
                start_window_cpu_usage = None
                start_window_disk_usage = None
                stop_window_disk_usage = idle_windows[0]["avg_disk"] if idle_windows else None
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

                # Decision logic based on adaptive_idle
                adaptive_idle, _ = calculate_dynamic_thresholds(values)
                if idle_windows and min([w["avg_cpu"] for w in idle_windows]) < adaptive_idle:
                    decision = "Safe to stop VM – true idle window found"
                else:
                    decision = "Monitor – no true idle detected (lowest window used)"

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
