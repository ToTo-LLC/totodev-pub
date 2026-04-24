# Case Study: Background Job Orchestration with PrimitiveEventLog

## The Business Scenario

You're building a SaaS platform that sends personalized email reports to clients. When a user clicks "Generate Report," the system needs to:

1. **Fetch** data from your analytics database
2. **Process** the data (calculate metrics, format for display)
3. **Notify** the user by sending the report via email

Each step can fail, and failures should trigger retries with exponential backoff. After 3 failed attempts, the job should be marked as abandoned and alert an admin.

### The Challenge

You need to track where each job is in the pipeline and debug failures quickly when they occur. Traditional solutions:

- **Database table with status column**: Works, but requires migrations, indexes, query optimization. Overkill for simple tracking.
- **Redis queue**: Great for queueing, but poor for historical debugging. "What happened to job #12345 last Tuesday?"
- **Full observability platform**: Expensive, complex setup, learning curve for the team.

### The PrimitiveEventLog Solution

Each job gets its own event log directory. File names tell the story at a glance. No database, no setup, just point at a folder and go.

## Modeling the Job Lifecycle

### Event Log Structure

Each job has a directory: `jobs/{job_id}/events/`

The event log uses just 4 simple labels to track the entire lifecycle:

```
jobs/
  job_12345/
    events/
      e001_START@JOB.yaml
      e002_START@FETCH.yaml
      e003_FINISH@FETCH.yaml
      e004_START@PROCESS.yaml
      e005_ERROR@PROCESS_FAIL.yaml         # First failure
      e006_START@PROCESS.yaml              # Retry
      e007_FINISH@PROCESS.yaml
      e008_START@NOTIFY.yaml
      e009_FINISH@NOTIFY.yaml
      e010_RESULT@SENT_2025-11-07T143215.yaml
      e011_FINISH@JOB.yaml
```

Just by looking at the file names, you can see:
- FETCH: started and finished successfully
- PROCESS: failed once (ERROR@PROCESS_FAIL), then retried and finished
- NOTIFY: started and finished successfully
- Job completed with RESULT

**Current stage?** Whatever has a START but no matching FINISH (nothing here - job done!)

### Event Labels (Just 4!)

- **`START`**: Mark beginning of a stage or job
  - Values: `JOB`, `FETCH`, `PROCESS`, `NOTIFY`
  
- **`FINISH`**: Mark successful completion of a stage or job
  - Values: `JOB`, `FETCH`, `PROCESS`, `NOTIFY`
  
- **`ERROR`**: Capture failure details
  - Values: `FETCH_FAIL`, `PROCESS_FAIL`, `NOTIFY_FAIL`, `UNKNOWN`
  
- **`RESULT`**: Final job outcome with timestamp
  - Values: `SENT_{timestamp}`, `ABANDONED_{timestamp}`

### Event Payloads

Each event can carry relevant data:

```yaml
# e005_ERROR@PROCESS_FAIL.yaml
error_message: "Division by zero in revenue calculation"
error_type: "ZeroDivisionError"
stack_trace: |
  File "processor.py", line 142, in calculate_metrics
    avg_revenue = total / count
worker_id: "worker-03"
attempt: 1
timestamp: "2024-11-07T14:32:15Z"
```

```yaml
# e007_FINISH@PROCESS.yaml
rows_processed: 15234
metrics_calculated: 12
processing_time_ms: 423
worker_id: "worker-03"
attempt: 2
timestamp: "2024-11-07T14:32:28Z"
```

```yaml
# e010_RESULT@SENT_2025-11-07T143215.yaml
email_sent_to: "user@example.com"
email_id: "msg_job_12345"
total_duration_seconds: 16.2
stages_completed: 3
retry_count: 1
```

## Code Example: Building the Job Processor

### Setup: Job Initialization

```python
from pathlib import Path
from datetime import datetime
from totodev_pub.primitive_event_log import PrimitiveEventLog
import time

class ReportJob:
    """Represents a background job for report generation."""
    
    def __init__(self, job_id: str, user_id: str, report_type: str):
        self.job_id = job_id
        self.user_id = user_id
        self.report_type = report_type
        
        # Each job gets its own event log
        job_dir = Path(f"./jobs/{job_id}")
        self.event_log = PrimitiveEventLog(
            event_dir=job_dir / "events",
            force=True  # Create directory immediately
        )
        
        # Record job creation
        self.event_log.create_event(
            "START", 
            "JOB",
            {
                "user_id": user_id,
                "report_type": report_type,
                "created_at": datetime.utcnow().isoformat()
            }
        )
```

### Worker: Processing the Job

```python
class ReportWorker:
    """Background worker that processes report jobs."""
    
    def __init__(self, worker_id: str):
        self.worker_id = worker_id
    
    def claim_job(self, job: ReportJob) -> bool:
        """Try to claim a job for processing."""
        values = job.event_log.latest_values()
        
        # Only claim if job created but not yet started processing
        if values.get("START") == "JOB" and values.get("FINISH") != "JOB":
            return True
        return False
    
    def process_with_retry(self, job: ReportJob, stage_func, stage_name: str):
        """Execute a stage with retry logic."""
        max_attempts = 3
        
        for attempt in range(1, max_attempts + 1):
            try:
                # Log START of stage
                job.event_log.create_event(
                    "START",
                    stage_name,
                    {
                        "worker_id": self.worker_id,
                        "attempt": attempt,
                        "started_at": datetime.utcnow().isoformat()
                    }
                )
                
                # Execute the actual work
                start_time = time.time()
                result = stage_func(job)
                elapsed = time.time() - start_time
                
                # Log FINISH of stage with results
                result["worker_id"] = self.worker_id
                result["attempt"] = attempt
                result["elapsed_seconds"] = round(elapsed, 2)
                result["completed_at"] = datetime.utcnow().isoformat()
                
                job.event_log.create_event(
                    "FINISH",
                    stage_name,
                    result
                )
                
                return result  # Success!
                
            except Exception as e:
                # Log error
                job.event_log.create_event(
                    "ERROR",
                    f"{stage_name}_FAIL",
                    {
                        "error_message": str(e),
                        "error_type": type(e).__name__,
                        "worker_id": self.worker_id,
                        "attempt": attempt,
                        "failed_at": datetime.utcnow().isoformat()
                    }
                )
                
                if attempt == max_attempts:
                    # Give up after max attempts
                    timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H%M%S")
                    job.event_log.create_event(
                        "RESULT",
                        f"ABANDONED_{timestamp}",
                        {
                            "reason": f"Failed after {max_attempts} attempts",
                            "stage": stage_name,
                            "final_error": str(e)
                        }
                    )
                    raise
                
                # Exponential backoff before retry
                time.sleep(2 ** attempt)
    
    def process_job(self, job: ReportJob):
        """Process all stages of a report job."""
        if not self.claim_job(job):
            print(f"Could not claim job {job.job_id}")
            return
        
        start_time = time.time()
        
        try:
            # Stage 1: Fetch data
            self.process_with_retry(job, self._fetch_data, "FETCH")
            
            # Stage 2: Process data
            self.process_with_retry(job, self._process_data, "PROCESS")
            
            # Stage 3: Notify user
            notify_result = self.process_with_retry(job, self._notify_user, "NOTIFY")
            
            # Mark final result with timestamp
            timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H%M%S")
            job.event_log.create_event(
                "RESULT",
                f"SENT_{timestamp}",
                {
                    "worker_id": self.worker_id,
                    "email_sent_to": notify_result.get("email_sent_to", ""),
                    "total_duration_seconds": time.time() - start_time,
                    "stages_completed": 3,
                    "retry_count": 0  # Could track actual retries
                }
            )
            
            # Mark overall job as finished
            job.event_log.create_event("FINISH", "JOB", {})
            
        except Exception as e:
            print(f"Job {job.job_id} failed: {e}")
    
    def _fetch_data(self, job: ReportJob) -> dict:
        """Fetch data from analytics database."""
        # Simulate database query
        # In real code: query analytics tables, join data, etc.
        return {
            "rows_fetched": 15234,
            "tables_queried": 3,
            "query_time_ms": 847
        }
    
    def _process_data(self, job: ReportJob) -> dict:
        """Process and calculate metrics."""
        # Simulate data processing
        # In real code: aggregations, calculations, formatting
        return {
            "rows_processed": 15234,
            "metrics_calculated": 12,
            "processing_time_ms": 423
        }
    
    def _notify_user(self, job: ReportJob) -> dict:
        """Send report email to user."""
        # Simulate email sending
        # In real code: format email, call SendGrid/SES, etc.
        return {
            "email_sent_to": job.user_id,
            "email_id": f"msg_{job.job_id}",
            "notification_type": "email"
        }
```

### Usage: Running Jobs

```python
# Create a job
job = ReportJob(
    job_id="job_12345",
    user_id="user@example.com",
    report_type="monthly_revenue"
)

# Process with a worker
worker = ReportWorker(worker_id="worker-03")
worker.process_job(job)

# Check values at any time
values = job.event_log.latest_values()
print(f"Current values: {values}")
# MappingProxyType({'START': 'NOTIFY', 'FINISH': 'JOB', 'ERROR': 'PROCESS_FAIL', 'RESULT': 'SENT_2025-11-07T143215'})

# Determine if job is complete
if values.get("FINISH") == "JOB":
    print("Job complete!")
elif values.get("START"):
    print(f"Currently running: {values.get('START')}")
else:
    print("Job not started")

# Review full history for debugging
print("\n=== Job History ===")
for event in job.event_log.events():
    print(f"{event.label_value} at {event.mtime}")
    data = event.contents()
    if data and 'error_message' in data.as_dict():
        print(f"  ERROR: {data['error_message']}")
```

### Admin Dashboard: Monitoring Jobs

```python
def monitor_jobs(jobs_dir: Path = Path("./jobs")):
    """Monitor all jobs and identify issues."""
    
    for job_dir in jobs_dir.iterdir():
        if not job_dir.is_dir():
            continue
        
        job_id = job_dir.name
        event_log = PrimitiveEventLog(event_dir=job_dir / "events")
        
        values = event_log.latest_values()
        started = values.get("START", None)
        finished = values.get("FINISH", None)
        result = values.get("RESULT", None)
        
        # Check if failed
        if result and result.startswith("ABANDONED"):
            print(f"⚠️  {job_id}: FAILED - needs attention")
            
            # Find the error details
            for event in event_log.events(label_glob="ERROR"):
                data = event.contents()
                if data:
                    print(f"   Error type: {event.value}")
                    print(f"   Message: {data.as_dict().get('error_message', 'Unknown')}")
                    break  # Show most recent error
        
        # Check if completed successfully (FINISH == JOB means job is done)
        elif finished == "JOB" and result and result.startswith("SENT"):
            print(f"✅ {job_id}: Success")
        
        # Still in progress (FINISH != JOB means not done yet)
        elif finished != "JOB" and started:
            print(f"⏳ {job_id}: In progress (running: {started})")
        
        else:
            print(f"❓ {job_id}: Unknown state")

# Run the monitor
monitor_jobs()
```

## Benefits of This Approach

### 1. **Zero Setup**
No database schema, no migrations, no Redis configuration. Just create a directory and start logging.

### 2. **Simple START/FINISH Model**
Only 4 labels (`START`, `FINISH`, `ERROR`, `RESULT`) cover the entire job lifecycle. Current stage = whatever has START but no matching FINISH. Easy to understand and query.

### 3. **Human-Browsable**
Open `jobs/job_12345/events/` in your file browser and instantly see the job's progression. Perfect for debugging.

### 4. **Git-Friendly**
Commit example event logs to your repo for test cases and documentation.

### 5. **Concurrent-Safe**
Multiple workers can safely create events without coordination. The sequence numbers prevent conflicts.

### 6. **Rich Debugging**
Every failure is captured with full context (error messages, stack traces, timing). No log aggregation needed.

### 7. **AI/LLM-Friendly**
Drop an event directory into an AI coding assistant and ask "What went wrong with this job?" The file names alone tell most of the story.

### 8. **Simple Monitoring**
Dashboard code is straightforward - just scan directories and check `latest_values()`. No query optimization needed.

## When NOT to Use This Approach

- **High-volume transactions**: Millions of events per second → use a real database
- **Complex queries**: Need to join across jobs or aggregate metrics → use PostgreSQL
- **Distributed systems**: Events across multiple servers → use centralized logging
- **Real-time dashboards**: Need sub-second updates → use Redis/pub-sub

## Conclusion

For background job tracking in Python applications, `PrimitiveEventLog` hits a sweet spot: simple enough to set up in 5 minutes, powerful enough to debug production issues, and maintainable enough that future developers will thank you.

The filesystem is your database. No overhead, no complexity, just files.

