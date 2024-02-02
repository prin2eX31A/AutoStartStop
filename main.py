#!/usr/bin/env python3
import os
import boto3
import botocore.exceptions
import datetime
import re
import logging
import time
import json
import base64

"""
Objective:
catering 3 different types of automations
1. auto start EC2 / RDS / EKS node group
2. auto stop EC2 / RDS / EKS node group
3. repeatedly stop RDS that boot up every 7 days

Resource:
1. ec2:instance
2. rds:db
3. eks:nodegroup

Tag Key:
- AutoStartStop

Time slots based on Tag Values:
1. OfficeHour = 8:30 - 18:30 (UTC+8) each weekdays
2. ExtendedOfficeHour1 = 8:30 - 21:30 each weekdays
3. ExtendedOfficeHour2 = 8:30 - 23:59 each weekdays
4. UpperHalf = 8:30 - 14:30 each weekdays
5. LowerHalf = 12:00 - 18:30 each weekdays
6. RecurringStop = auto starting RDS at 1:00am on every saturday, and then auto stop RDS at 3:00am every Saturday (finally align the shutdown time)

Regional deployment for the solution:
1. since the global endpoint of sts for lambda to assume role may create a credential that not allow to 
    access non-default regions(i.e. HK and Jarkata)
2. workaround: deploy the solution on every designated regions, so that lambda can use the local sts endpoint to assume role

Reasons of filtering:
1. No repeating start of EC2 to prevent reboot actions
2. No stopping RDS with upgrading or modifying states

Role using by Lambda:
- dcp-svc-role-lambda-auto-start-stop

Role using by EventBridge:
- dcp-svc-role-events-auto-start-stop

Not using ASG schedule scaling policy:
1. prevent configuration conflicts with IaC / console
2. keep flexibility, passing the latest node group scaling config to a tag each time

Tag EKS nodegroup (key = 'nodegroup_scaling')
- Record the scaling config of node group before shut down

Tag value encoded with base64:
1. aws tags do not accept JSON format
2. simplify the process on encode / decode by passing original JSON payload

Limitation:
- since a page of api response can only contain not more than 100 result, 
    over 100 result will be omitted in this function

"""

def lambda_handler(event, context):
    """
    :param event: { "details": { "automation": "start", "resource": "rds", "tag key": "AutoStartStop", "tag value": "ExtendedOfficeHour2" } }
    """
    print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | Event: {event}')

    # Use profile with AWS CLI while run locally:
    # session = boto3.session.Session(profile_name='CORESHAREDTEST-OrgAdmin-6428')

    # Use lambda role while run remotely:
    # state the source credential / source session
    session = boto3.session.Session()

    region = os.environ['AWS_REGION']    #can only be used on lambda
    # region = 'ap-southeast-1'

    def get_tagged_instance():

        # ResourceTaggingAPI is casted as tagapi
        tagapi = session.client('resourcegroupstaggingapi', region_name=region)

        print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | session created in {region} for searching tagged ec2')

        try:

            tagged_resources = tagapi.get_resources(
                TagFilters = [
                    {
                        'Key': event['details']['tag key'],
                        'Values': [
                            event['details']['tag value']
                        ]
                    }
                ],
                ResourcesPerPage = 100,
                ResourceTypeFilters = [
                    "ec2:instance"
                ]
            )

            print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | request sent')
            # print(tagged_resources)
            '''
            expected out put
            {'PaginationToken': '', 'ResourceTagMappingList':[{'ResourceARN':'String', 'Tags':{'Key': 'key', 'Value': 'tag value'}}, {others...}]}
            '''
        except botocore.exceptions.ClientError as err:
            logging.error("Couldn't search with the tag key %s. Here's why: %s: %s", event['details']['tag value'],
                          err.response['Error']['Code'], err.response['Error']['Message'])
            raise

        instanceID_list = []

        for resources in tagged_resources["ResourceTagMappingList"]:

            ec2arn = resources["ResourceARN"].split("instance/")

            instanceID_list.append(ec2arn[1])

        # logging.CRITICAL()

        return instanceID_list

    def get_tagged_dbinstance():

        # ResourceTaggingAPI is casted as tagapi
        tagapi = session.client('resourcegroupstaggingapi', region_name=region)

        print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | session created in {region} for searching tagged rds')

        try:

            tagged_resources = tagapi.get_resources(
                # IncludeComplianceDetails = True,    #commented out since not applicable
                # ExculdeComplianceResource = True,    #commented out since not applicable
                # PaginationToken='', #commented out since no pagination token is passed here
                TagFilters = [
                    {
                        'Key': event['details']['tag key'],
                        'Values': [
                            event['details']['tag value']
                        ]
                    }
                ],
                # TagsPerPage = 100,  #commented out since no. of tag per page is not applicable
                ResourcesPerPage = 100,
                ResourceTypeFilters = [
                "rds:db"
                ]
            )

            print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | request sent')
            # print(tagged_resources)
            '''
            expected output
            {'PaginationToken': '', 'ResourceTagMappingList':[{'ResourceARN':'String', 'Tags':{'Key': 'key', 'Value': 'tag value'}}, {others...}]}
            '''

        except botocore.exceptions.ClientError as err:
            logging.error("Couldn't search with the tag key %s. Here's why: %s: %s", event['details']['tag value'],
                          err.response['Error']['Code'], err.response['Error']['Message'])
            raise

        dbinstance_list = []

        for resources in tagged_resources["ResourceTagMappingList"]:

            rdsdbarn = resources["ResourceARN"].split(":db:")

            dbinstance_list.append(rdsdbarn[1])

        return dbinstance_list

    def get_tagged_ekscluster():

        # ResourceTaggingAPI is casted as tagapi
        tagapi = session.client('resourcegroupstaggingapi', region_name=region)

        print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | session created in {region} for searching tagged eks node groups')

        try:

            tagged_resources = tagapi.get_resources(
                # IncludeComplianceDetails = True,    #commented out since not applicable
                # ExculdeComplianceResource = True,    #commented out since not applicable
                # PaginationToken='', #commented out since no pagination token is passed here
                TagFilters=[
                    {
                        'Key': event['details']['tag key'],
                        'Values': [
                            event['details']['tag value']
                        ]
                    }
                ],
                # TagsPerPage = 100,  #commented out since no. of tag per page is not applicable
                ResourcesPerPage = 100,
                ResourceTypeFilters = [
                    "eks:nodegroup"
                ]
            )

            print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | request sent')
            # print(tagged_resources)
            '''
            expected output
            {'PaginationToken': '', 'ResourceTagMappingList':[{'ResourceARN':'String', 'Tags':{'Key': 'key', 'Value': 'tag value'}}, {others...}]}
            '''

        except botocore.exceptions.ClientError as err:
            logging.error("Couldn't search with the tag key %s. Here's why: %s: %s", event['details']['tag value'],
                          err.response['Error']['Code'], err.response['Error']['Message'])
            raise

        eksnodegroup_list = []

        for resources in tagged_resources["ResourceTagMappingList"]:

            nodegrouparn = resources["ResourceARN"].split(":nodegroup/")
            nodegroup = nodegrouparn[1].split("/")
            nodegroupinfo = {"cluster": nodegroup[0], "nodegroupname": nodegroup[1]}


            eksnodegroup_list.append(nodegroupinfo)

        return eksnodegroup_list

    def auto_start_instance(instances):

        ec2 = session.client('ec2', region_name=region)

        #core list for instance to be stopped
        instances_should_be_started = instances.copy()
        """
        If using only "instances_should_be_stopped = instances", the function will modify the original list of instances
        So, need to use "instances_should_be_stopped = instances.copy()" can clone an individual list that no affecting the original list
        """

        print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | list of instances: {instances_should_be_started}')

        for instance in instances:

            ec2_states = ec2.describe_instance_status(
                InstanceIds=[instance]
            )

            print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | ec2 {instance} states: {ec2_states}')

            """
            expected output
            {'InstanceStatuses': [{..., 'InstanceId': 'string','InstanceState': {'Code': 123,'Name': '<running / stopped>'},...}, ...]}
            
            *** if the instance had already been stopped, no instance statuses will be returned
                i.e. {'InstanceStatuses': []}
            """

            # if the list of instance statuses is not empty (which means not False in python), i.e. instance had been started, so it shouldn't be start again
            if ec2_states['InstanceStatuses']:

                instances_should_be_started.remove(instance)
                print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | Instance id: {instance} has already been started. \n the list now will be: {instances_should_be_started}')

            else:
                print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | No changes to instance list: {instances_should_be_started}')

        # check instance list, since if length = 0, no further actions should be taken
        if len(instances_should_be_started) == 0:
            print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | No instances should be started')

        # if length > 0, startec2 instance in lists
        elif len(instances_should_be_started) > 0:
            startec2 = ec2.start_instances(
                InstanceIds=instances_should_be_started
            )

            # print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | {startec2}')
            """
            expected output
            {'StartingInstances': [{'CurrentState': {'Code': <code>,'Name': 'stopping'}, 'InstanceId': 'string', 'PreviousState': {'Code': <code>, 'Name': 'stopping'}}, ...
            """

            responses_started_ec2 = []

            #obtain the list of instances that is started
            for ec2 in startec2['StartingInstances']:

                responses_started_ec2.append(ec2['InstanceId'])
            return responses_started_ec2

    def auto_stop_instance(instances):

        ec2 = session.client('ec2', region_name=region)

        #core list for instance to be stopped
        instances_should_be_stopped = instances.copy()
        """
        If using only "instances_should_be_stopped = instances", the function will modify the original list of instances
        So, need to use "instances_should_be_stopped = instances.copy()" can clone an individual list that no affecting the original list
        """

        print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | list of instance: {instances_should_be_stopped}')

        for instance in instances:

            ec2_states = ec2.describe_instance_status(
                InstanceIds=[instance]
            )

            # print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | ec2 {instance} states: {ec2_states}')
            """
            expected output
            {'InstanceStatuses': [{..., 'InstanceId': 'string','InstanceState': {'Code': 123,'Name': '<running / stopped>'},...}, ...]}
            
            *** if the instance had already been stopped, no instance statuses will be returned
                i.e. {'InstanceStatuses': []}
            """

            if ec2_states['InstanceStatuses'] == False:

                instances_should_be_stopped.remove(instance)
                print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | Instance id: {instance} has already been stopped. \n the list now will be: {instances_should_be_stopped}')

            else:
                print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | No changes to instance list: {instances_should_be_stopped}')

        # check instance list, since if length = 0, no further actions should be taken
        if len(instances_should_be_stopped) == 0:
            print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | No instances should be stopped')

        # if length > 0, stopec2 instance in lists
        elif len(instances_should_be_stopped) > 0:
            stopec2 = ec2.stop_instances(
                InstanceIds=instances_should_be_stopped
            )

            print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | {stopec2}')
            """
            expected output
            {'StoppingInstances': [{'CurrentState': {'Code': <code>,'Name': 'stopping'}, 'InstanceId': 'string', 'PreviousState': {'Code': <code>, 'Name': 'stopping'}}, ...
            """

            responses_stopped_ec2 = []

            #obtain the list of instances that is stopped
            for ec2 in stopec2['StoppingInstances']:

                responses_stopped_ec2.append(ec2['InstanceId'])
            return responses_stopped_ec2

    def auto_start_dbinstance(dbinstances):

        rds = session.client('rds', region_name=region)

        # create a new list for dbinstances
        dbidentifier_should_be_started = dbinstances.copy()
        print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | DB list: {dbidentifier_should_be_started} \n')

        # check each rds instance details
        for rds_dbidentifier in dbinstances:

            checkrds = rds.describe_db_instances(
                DBInstanceIdentifier=rds_dbidentifier
            )

            # print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | {checkrds})
            """
            expected output
            {'Marker': 'string', 'DBInstances': [{'DBInstanceIdentifier': 'string',..., 'DBInstanceStatus': '<available | stopped>', 'Engine': <mysql | sqlserver-se>, 'MultiAZ': <True / False>,...}]}
        
            """
            # Only start up the RDS that fully stopped, if RDS is not in stopped state, will be filtered out
            if checkrds['DBInstances'][0]['DBInstanceStatus'] != 'stopped':
                dbidentifier_should_be_started.remove(rds_dbidentifier)
                print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | DB Instance Name: {rds_dbidentifier} has already been started. \n the list now will be: {dbidentifier_should_be_started}')
            else:
                print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | DB List: \n{dbidentifier_should_be_started}')

            check_rds_details = {}
            check_rds_details['DB'] = checkrds['DBInstances'][0]['DBInstanceIdentifier']
            check_rds_details['DBMS Engine'] = checkrds['DBInstances'][0]['Engine']
            check_rds_details['MultiAZ Deployment'] = checkrds['DBInstances'][0]['MultiAZ']
            """
            expected pattern:
            {'DB': '<string>', 'DBMS Engine': '<mysql | sqlserver-se | sqlserver-web>', 'MultiAZ Deployment': True | False} 
            """

            print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | RDS named {checkrds["DBInstances"][0]["DBInstanceIdentifier"]}:')
            print(check_rds_details, "\n")

        response_started_rds = []

        # check the list, if length = 0, no requests should be sent
        if len(dbidentifier_should_be_started) == 0:
            print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | No db instances should be started')

        # check the list, if length > 0, send request to stop designated RDS
        elif len(dbidentifier_should_be_started) > 0:

            for rds_dbidentifier in dbidentifier_should_be_started:
                startrds = rds.start_db_instance(
                    DBInstanceIdentifier=rds_dbidentifier
                )

                print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | Starting {rds_dbidentifier}:\n{startrds}')

                response_started_rds.append(startrds['DBInstance']['DBInstanceIdentifier'])

        return response_started_rds

    def auto_stop_dbinstance(dbinstances):

        rds = session.client('rds', region_name=region)

        # create a new list for dbinstances
        dbidentifier_should_be_stopped = dbinstances.copy()
        print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | DB list: {dbidentifier_should_be_stopped} \n')

        for rds_dbidentifier in dbinstances:

            checkrds = rds.describe_db_instances(
                DBInstanceIdentifier=rds_dbidentifier
            )

            print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | {checkrds}')
            """
            expected output
            {'Marker': 'string', 'DBInstances': [{'DBInstanceIdentifier': 'string',..., 'DBInstanceStatus': '<available | stopped>', 'Engine': <mysql | sqlserver-se>, 'MultiAZ': <True / False>,...}]}
        
            """
            # multi-az deployed SQL server RDS should not be able to stop, so need to filter out those db instances
            if checkrds['DBInstances'][0]['MultiAZ'] is True and re.search('^sqlserver.*', checkrds['DBInstances'][0]['Engine']) != None:
                dbidentifier_should_be_stopped.remove(rds_dbidentifier)
                # using object.group() to show the string matching with the pattern stated in re.search()
                print(re.search('^sqlserver.*', checkrds['DBInstances'][0]['Engine']).group(), '\n')
                print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | DB Instance Name: {rds_dbidentifier} is an active-active SQL Server, which cannot be stopped. \n the list now will be: {dbidentifier_should_be_stopped}')
            #only turn off available RDS, since 'upgrading' or 'starting' RDS should not be distrubed
            elif checkrds['DBInstances'][0]['DBInstanceStatus'] != 'available':
                dbidentifier_should_be_stopped.remove(rds_dbidentifier)
                print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | DB Instance Name: {rds_dbidentifier} has already been stopped. \n the list now will be: {dbidentifier_should_be_stopped}')
            else:
                print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | DB List: \n{dbidentifier_should_be_stopped}')

            check_rds_details = {}
            check_rds_details['DB'] = checkrds['DBInstances'][0]['DBInstanceIdentifier']
            check_rds_details['DBMS Engine'] = checkrds['DBInstances'][0]['Engine']
            check_rds_details['MultiAZ Deployment'] = checkrds['DBInstances'][0]['MultiAZ']
            """
            expected pattern:
            {'DB': '<string>', 'DBMS Engine': '<mysql | sqlserver-se | sqlserver-web>', 'MultiAZ Deployment': True | False} 
            """

            print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | RDS named {checkrds["DBInstances"][0]["DBInstanceIdentifier"]}:')
            print(check_rds_details, "\n")

        response_stopped_rds = []

        # check the list, if length = 0, no requests should be sent
        if len(dbidentifier_should_be_stopped) == 0:
            print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | No db instances should be stopped')

        # check the list, if length > 0, send request to stop designated RDS
        elif len(dbidentifier_should_be_stopped) > 0:

            for rds_dbidentifier in dbidentifier_should_be_stopped:
                stoprds = rds.stop_db_instance(
                    DBInstanceIdentifier=rds_dbidentifier
                    )

                print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | Stopping {rds_dbidentifier}:\n{stoprds}')

                response_stopped_rds.append(stoprds['DBInstance']['DBInstanceIdentifier'])

        return response_stopped_rds

    def auto_start_eks_nodegroup(nodegroups):

        eks = session.client('eks', region_name=region)

        print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | session created in {region} for eks actions')

        started_node_groups = []

        for nodegroup in nodegroups:

            try:

                nodegroup_config = eks.describe_nodegroup(
                    clusterName=nodegroup["cluster"],
                    nodegroupName=nodegroup["nodegroupname"]
                )

            except botocore.exceptions.ClientError as err:
                logging.error("Error on describing node group %s. Here's why: %s: %s", nodegroup["nodegroupname"],
                              err.response['Error']['Code'], err.response['Error']['Message'])
                raise

            # print(nodegroup_config)
            '''
            expected output:
            {'nodegroup': {'nodegroupName': 'string', 'nodegroupArn': 'arn:aws:eks:<region>:<AWS ID>:nodegroup/<Cluster Name>/<Nodegroup Name>/<Nodegroup ID>', 'clusterName': 'string', 'scalingConfig': {'minSize': 1, 'maxSize': 5, 'desiredSize': 2}, 'tags': {'AutoStartStop': 'OfficeHour'}, ...}}
            '''

            print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | Is {nodegroup_config["nodegroup"]["nodegroupName"]} got tagged with scaling config?", nodegroup_config["nodegroup"]["tags"].get("nodegroup_scaling")')

            if nodegroup_config['nodegroup']['tags'].get('nodegroup_scaling') == None:

                print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | Error: {nodegroup_config["nodegroup"]["nodegroupName"]} has no scaling config tag')

            elif nodegroup_config['nodegroup']['tags'].get('nodegroup_scaling'):

                decrypted_payload = json.loads(bytes.decode(base64.b64decode(bytes(nodegroup_config['nodegroup']['tags']['nodegroup_scaling'], 'utf-8'))))
                print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | the json payload is {decrypted_payload}, and encoded with base64 = {nodegroup_config["nodegroup"]["tags"]["nodegroup_scaling"]}')

                try:

                    start_nodegroup = eks.update_nodegroup_config(
                        clusterName=nodegroup["cluster"],
                        nodegroupName=nodegroup["nodegroupname"],
                        scalingConfig=decrypted_payload
                    )

                except botocore.exceptions.ClientError as err:
                    logging.error("Error on updating node group %s. Here's why: %s: %s", nodegroup["nodegroupname"],
                                  err.response['Error']['Code'], err.response['Error']['Message'])
                    raise

                # print(start_nodegroup)
                print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | {nodegroup["nodegroupname"]} has been started')

                started_node_groups.append(nodegroup)

        return started_node_groups

    def auto_stop_eks_nodegroup(nodegroups):

        eks = session.client('eks', region_name=region)

        print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | session created in {region} for eks actions')

        stopped_node_groups = []

        for nodegroup in nodegroups:

            try:

                nodegroup_config = eks.describe_nodegroup(
                    clusterName=nodegroup["cluster"],
                    nodegroupName=nodegroup["nodegroupname"]
                )

            except botocore.exceptions.ClientError as err:
                logging.error("Error on describing node group %s. Here's why: %s: %s", nodegroup["nodegroupname"],
                              err.response['Error']['Code'], err.response['Error']['Message'])
                raise

            # print(nodegroup_config)
            '''
            expected output:
            {'nodegroup': {'nodegroupName': 'string', 'nodegroupArn': 'arn:aws:eks:<region>:<AWS ID>:nodegroup/<Cluster Name>/<Nodegroup Name>/<Nodegroup ID>', 'clusterName': 'string', 'scalingConfig': {'minSize': 1, 'maxSize': 5, 'desiredSize': 2}, 'tags': {'AutoStartStop': 'OfficeHour'}, ...}}
            '''

            print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | Is {nodegroup_config["nodegroup"]["nodegroupName"]} got tagged with scaling config?", nodegroup_config["nodegroup"]["tags"].get("nodegroup_scaling")')

            json_payload = json.dumps(nodegroup_config['nodegroup']['scalingConfig'])
            encrypted_payload = base64.b64encode(bytes(json_payload, 'utf-8'))
            encrypted_payload_str = str(encrypted_payload).split("\'")[1]
            # print("encoded: ", encrypted_payload_str)
            decrypted_payload_verified = bytes.decode(base64.b64decode(encrypted_payload), 'utf-8')
            # print("decoded: ", decrypted_payload)

            print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | the json payload is {decrypted_payload_verified}, and encoded with base64 = {encrypted_payload_str}')

            if nodegroup_config['nodegroup']['tags'].get('nodegroup_scaling') == None:

                try:
                    tag_config_to_nodgroup = eks.tag_resource(
                        resourceArn=nodegroup_config['nodegroup']['nodegroupArn'],
                        tags={
                        'nodegroup_scaling': f'{encrypted_payload_str}'
                        }
                    )
                    print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | tag on {nodegroup_config["nodegroup"]["nodegroupName"]} had been added')

                except botocore.exceptions.ClientError as err:
                    logging.error("Error on tagging node group %s. Here's why: %s: %s", nodegroup["nodegroupname"],
                                  err.response['Error']['Code'], err.response['Error']['Message'])
                    raise

            elif nodegroup_config['nodegroup']['tags'].get('nodegroup_scaling'):

                try:
                    update_tag_config_to_nodgroup = eks.tag_resource(
                        resourceArn=nodegroup_config['nodegroup']['nodegroupArn'],
                        tags={
                        'nodegroup_scaling': f'{encrypted_payload_str}'
                        }
                    )
                    print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | tag on {nodegroup_config["nodegroup"]["nodegroupName"]} had been updated')

                except botocore.exceptions.ClientError as err:
                    logging.error("Error on tagging node group %s. Here's why: %s: %s", nodegroup["nodegroupname"],
                                  err.response['Error']['Code'], err.response['Error']['Message'])
                    raise

            try:
                stop_nodegroup = eks.update_nodegroup_config(
                    clusterName=nodegroup["cluster"],
                    nodegroupName=nodegroup["nodegroupname"],
                    scalingConfig={
                        'minSize': 0,
                        'maxSize': 1,
                        'desiredSize': 0
                    },
                )

            except botocore.exceptions.ClientError as err:
                logging.error("Error on updating node group %s. Here's why: %s: %s", nodegroup["nodegroupname"],
                              err.response['Error']['Code'], err.response['Error']['Message'])
                raise

            # print(stop_nodegroup)
            print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | {nodegroup["nodegroupname"]} has been stopped')

            stopped_node_groups.append(nodegroup)

        return stopped_node_groups

    def check_payload_tag(payload):
        #payload = events
        tagKey = ['DCP/AutoStartStop']
        tagValues = ['OfficeHour', 'ExtendedOfficeHour1', 'ExtendedOfficeHour2', 'UpperHalf', 'LowerHalf', 'RecurringStop']
        try:
            tagKey_exist = tagKey.index(payload['details']['tag key'])
        except ValueError:
            print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | Payload ValueError, "tag key": \"{payload["details"]["tag key"]}\" is not a valid value')
            return {"details": payload["details"], "error": "Invalid Tag Key"}

        try:
            tagValue_exis = tagValues.index(payload['details']['tag value'])
        except ValueError:
            print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | Payload ValueError, "tag value": \"{payload["details"]["tag value"]}\" is not a valid value')
            return {"details": payload["details"], "error": "Invalid Tag Value"}
        else:
            return {"details": payload["details"], "error": ""}


    def response(action, list):
        final_result = {
            'Status': 'Successful',
            'AWS_ID': context.invoked_function_arn.split(":")[4],
            'Time': datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M"),
            'Action': action,
            'ResourceList': list
        }
        return json.dumps(final_result)


    # ******      main function started      ******

    event = check_payload_tag(event)

    if event['error'] == 'Invalid Tag Key' or event['error'] == 'Invalid Tag Value':
        print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | Event: {event} is not valid')
        return json.dumps({
            'Status': 'Failed',
            'AWS_ID': context.invoked_function_arn.split(":")[4],
            'Time': datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")
        })

    # stop ec2
    if event['details']['automation'].lower() == 'stop' and event['details']['resource'] == 'ec2':
        stopped_ec2 = auto_stop_instance(get_tagged_instance())
        return response('Stop EC2 instance', stopped_ec2)

    # start ec2
    elif event['details']['automation'].lower() == 'start' and event['details']['resource'] == 'ec2':
        started_ec2 = auto_start_instance(get_tagged_instance())
        return response('Start EC2 instance', started_ec2)

    # stop RDS
    elif event['details']['automation'].lower() == 'stop' and event['details']['resource'] == 'rds':
        stopped_rds = auto_stop_dbinstance(get_tagged_dbinstance())
        return response('Stop RDS', stopped_rds)

    # start RDS
    elif event['details']['automation'].lower() == 'start' and event['details']['resource'] == 'rds':
        started_rds = auto_start_dbinstance(get_tagged_dbinstance())
        return response('Start RDS', started_rds)

    # stop EKS node group
    elif event['details']['automation'].lower() == 'stop' and event['details']['resource'] == 'eks':
        stopped_nodegroups = auto_stop_eks_nodegroup(get_tagged_ekscluster())
        return response('Stop EKS node group', stopped_nodegroups)

    # start EKS node group
    elif event['details']['automation'].lower() == 'start' and event['details']['resource'] == 'eks':
        started_nodegroups = auto_start_eks_nodegroup(get_tagged_ekscluster())
        return response('Start EKS node group', started_nodegroups)

    else:
        print(f'[{datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M")}] | Event: {event} is not valid')
        return json.dumps({
            'Status': 'Failed',
            'AWS_ID': context.invoked_function_arn.split(":")[4],
            'Time': datetime.datetime.now(tz=None).strftime("%d %b %Y - %H:%M"),
        })

    # next_Token = ''
    # while next_Token != None:
    #     resources = []
    #     resources.clear()
    #     get_inventory = get_resource_list_from_config(resource_type,next_Token)
    #     print(get_inventory)
    #     next_Token = get_inventory[1]
    #
    #     if not get_inventory[0]:
    #         print(f'no resource in this type')
    #         continue
    #
    #     print(f'list is not empty')
    #
    #     if next_Token == '':
    #         print('next token is empty')
    #     else:
    #         print(f'next token is {next_Token}')
    #
    #     for resource in get_inventory[0]:
    #         resources.append(resource)
    #
    #     resource_list = search_for_resource_details(resources)
    #
    #     for object in resource_list:
    #         final_resource_list.append(object)

if __name__ == "__main__":
    #lambda_handler({"details": {"automation": "stop", "resource": "eks", "tag key": "AutoStartStop", "tag value": "OfficeHour"}}, {})
    lambda_handler({},{})