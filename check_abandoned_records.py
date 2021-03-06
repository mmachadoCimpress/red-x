import json
import boto3
import dns.resolver
import gitlab
from datetime import datetime

# Load configuration from the EC2 Parameter Store
# Transforms: /red-x/gitlab/token
# Into: {'red-x': {'gitlab': {'token': 'value'}}}
def load_config(ssmPath):
    ssm = boto3.client('ssm')
    resp = ssm.get_parameters_by_path(
        Path = ssmPath,
        Recursive=True,
        WithDecryption=True
    )
    config = {}

    for param in resp['Parameters']:
        path = param['Name'].split('/')
        current_level = config
        for level in path:
            if(level == '' or level == 'red-x'):
                continue
            if(level not in current_level):
                current_level[level] = {}
            if(level == path[-1]):
                current_level[level] = param['Value']
            else:
                current_level = current_level[level]
    return config

# Open or close GitLab issues based on delegation errors discovered by red-x.
# Opens an issue in the configured project for delegation errors and closes
# any open issues when it no longer identifies that error.
def notify_gitlab_issues(config, errors):
    # Load up all open issues in the configured project with label 'red-x'.
    gl = gitlab.Gitlab(config['gitlab']['endpoint'], config['gitlab']['token'], api_version=4)
    project = gl.projects.get(config['gitlab']['project'])
    issues = project.issues.list(labels=['red-x', 'record'], state='opened')
    zones_with_issues = [i.title for i in issues]

    for error in errors:
        # This error already has an issue
        if f"{error} abandoned record" in zones_with_issues:
            print(f"ALREADY FILED! {error}! Skipping")
            zones_with_issues.remove(f"{error} abandoned record")
        # This error needs a new issue created
        else:
            error_json = json.dumps(errors[error], indent=1)
            print(f"FILING: {error}!")
            issue = project.issues.create({'title': f"{error} abandoned record",
                               'description': f"""```
{error_json}
```""",
                               'labels': ['red-x', 'record']})

    # These issues no longer have a delegation error associated with them
    # and can be closed.
    for leftover in zones_with_issues:
        print(f"CLOSING ISSUE: {leftover}")
        issue = [x for x in issues if x.title == leftover][0]
        issue.notes.create({"body": "Subsequent runs of red-x no longer see this domain as an issue. Automatically closing ticket."})
        issue.state_event = "close"
        issue.save()

def eligible_cname(record):
    if 'ResourceRecords' in record and ('elasticbeanstalk.com' in record['ResourceRecords'][0]['Value'] or 'cloudfront.net' in record['ResourceRecords'][0]['Value']):
        return True
    return False

def eligible_alias(record):
    if 'AliasTarget' in record and ('elasticbeanstalk.com' in record['AliasTarget']['DNSName'] or 'cloudfront.net' in record['AliasTarget']['DNSName']):
        return True
    return False

# Send a summary of results to a configured SNS topic
def notify_sns_topic(config, errors):
    if len(errors) == 0:
        print("No record errors, not sending SNS notification...")
        return

    notification_time = str(datetime.now())
    sns = boto3.client('sns')
    error_text = json.dumps(errors, indent=2)
    sns.publish(
        TargetArn=config['sns']['topic'],
        Subject=f"Red-X Record Errors @ {notification_time}",
        Message=json.dumps({'default': f"""
Red-X has run and found the following DNS records pointing to inactive elasticbeanstalk or cloudfront domains. You should take action to prevent domain hijacking!

""" + error_text}),
        MessageStructure='json'
    )

def handler(event, context):
    config = load_config('/red-x/')
    r53 = boto3.client('route53')
    zone_id = config['route53']['zoneId']

    records = []
    nextName = None
    nextType = None

    # Fetch all records in the requested hosted zone
    while True:
        if nextName and nextType:
            response = r53.list_resource_record_sets(
                HostedZoneId = zone_id,
                StartRecordName = nextName,
                StartRecordType = nextType
            )
        else:
            response = r53.list_resource_record_sets(
                HostedZoneId = zone_id
            )

        records = records + response['ResourceRecordSets']

        if 'NextRecordName' in response and 'NextRecordType' in response:
            nextName = response['NextRecordName']
            nextType = response['NextRecordType']
        else:
            break

    # Discard everything except beanstalk-related records
    eligible_cnames = [{'name': x['Name'], 'value': x['ResourceRecords'][0]['Value'], 'type': x['Type']} for x in records if eligible_cname(x)]
    eligible_aliases = [{'name': x['Name'], 'value': x['AliasTarget']['DNSName'], 'type': x['Type']} for x in records if eligible_alias(x)]
    eligible_records = eligible_cnames + eligible_aliases

    violating_records = {}

    resolver = dns.resolver.Resolver(configure=False)
    resolver.timeout = 5

    # For each record pointing to beanstalk
    for record in eligible_records:
        violations = []
        if record['type'] == 'CNAME':
            violations.append(f"WARN: You should prefer A ALIAS over CNAME for {record['name']}")
        try:
            answer = dns.resolver.query(record['value'])
            print(f"OK: {record['name']}: {', '.join(str(x) for x in answer)}")
        except dns.resolver.NXDOMAIN:
            violations.append(f"CRIT: {record['name']} points to non-existent beanstalk name: {record['value']}")
        
        if len(violations) > 0:
            violating_records[record['name']] = violations

    # Open or close GitLab issues for these abandoned records.
    if('gitlab' in config):
        notify_gitlab_issues(config, violating_records)

    # Notify an SNS topic of all abandoned records.
    if('sns' in config):
        notify_sns_topic(config, violating_records)

    return {
        "message": "Completed checking for abandoned records.",
        "errors": violating_records
    }
