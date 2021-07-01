import time
import os
import logging
import slack
from json import load
from enum import Enum
from supervisor.src.job_create import JobCreate
from supervisor.src.job_find import JobFind
from postgres.src.pg_utils import PGUtils
from common.logging import LoggingUtil


class JobStatus(int, Enum):
    """
    Class that stores the job status constants
    """
    # new job status
    new = 1

    # file staging statuses
    staging_running = 10
    staging_complete = 20

    # obs_mod supp statuses
    obs_mod_running = 30
    obs_mod_complete = 40

    # geo tiff statuses
    run_geo_tiff_running = 50
    run_geo_tiff_complete = 60

    # mbtile statuses, zoom level 0 to 9
    compute_mbtiles_0_9_running = 70
    compute_mbtiles_0_9_complete = 80

    # mbtile statuses, zoom level 10
    compute_mbtiles_10_running = 80
    compute_mbtiles_10_complete = 90

    # mbtile statuses, zoom level 11
    compute_mbtiles_11_running = 100
    compute_mbtiles_11_complete = 110

    # mbtile statuses, zoom level 12
    compute_mbtiles_12_running = 120
    compute_mbtiles_12_complete = 130

    # load geoserver statuses
    load_geo_server_running = 140
    load_geo_server_complete = 150

    # final staging operations statuses
    final_staging_running = 160
    final_staging_complete = 170

    # overall status indicators
    warning = 9999
    error = -1


class JobType(str, Enum):
    """
    Class that stores the job type constants
    """
    staging = 'staging',
    obs_mod = 'obs-mod',
    run_geo_tiff = 'run-geo-tiff',
    compute_mbtiles_0_9 = 'compute-mbtiles-0-9',
    compute_mbtiles_10 = 'compute-mbtiles-10',
    compute_mbtiles_11 = 'compute-mbtiles-11',
    compute_mbtiles_12 = 'compute-mbtiles-12',
    load_geo_server = 'load-geo-server',
    final_staging = 'final-staging',
    error = 'error'
    other_1 = 'TBD'
    complete = 'complete'


class APSVizSupervisor:
    """
    Class for the APSViz supervisor

    """

    # the list of pending runs. this stores all job details of the run
    run_list = []

    def __init__(self):
        """
        inits the class
        """
        # load the run configuration params
        self.k8s_config: dict = self.get_config()

        # create a job creator object
        self.k8s_create = JobCreate()

        # create a job status finder object
        self.k8s_find = JobFind()

        # create the postgres access object
        self.pg_db = PGUtils()

        # get the log level and directory from the environment.
        # level comes from the container dockerfile, path comes from the k8s secrets
        log_level: int = int(os.getenv('LOG_LEVEL', logging.INFO))
        log_path: str = os.getenv('LOG_PATH', os.path.join(os.path.dirname(__file__), 'logs'))

        # create the dir if it does not exist
        if not os.path.exists(log_path):
            os.mkdir(log_path)

        # create a logger
        self.logger = LoggingUtil.init_logging("APSVIZ.APSVizSupervisor", level=log_level, line_format='medium', log_file_path=log_path)

        # instantiate slack connectivity
        self.slack_client = slack.WebClient(token=os.getenv('SLACK_ACCESS_TOKEN'))
        self.slack_channel = os.getenv('SLACK_CHANNEL')

    # TODO: make this a common function
    @staticmethod
    def get_config() -> dict:
        """
        gets the run configuration

        :return: Dict, baseline run params
        """

        # get the supervisor config file path/name
        config_name = os.path.join(os.path.dirname(__file__), '..', 'supervisor_config.json')

        # open the config file
        with open(config_name, 'r') as json_file:
            # load the config items into a dict
            data: dict = load(json_file)

        # return the config data
        return data

    def run(self):
        """
        endless loop finding task requests and create k8s jobs to complete them
        :return:
        """
        # set counter that indicates nothing was done
        no_activity_counter: int = 0

        # until the end of time
        while True:
            # get the incomplete runs from the database
            self.get_incomplete_runs()

            # reset the activity flag
            no_activity: bool = True

            # for each run returned from the database
            for run in self.run_list:
                # catch cleanup exceptions
                try:
                    # skip this job if it is complete
                    if run['job-type'] == JobType.complete:
                        run['status_prov'] += ', Run complete'
                        self.pg_db.update_job_status(run['id'], run['status_prov'])

                        # add a comment on overall pass/fail
                        if run['status_prov'].find('Error') == -1:
                            msg = f'*completed successfully*.'
                        else:
                            msg = f"*completed unsuccessfully*.\nRun provenance: {run['status_prov']}."

                        # send the message
                        self.send_slack_msg(run['id'], msg, run['instance_name'])

                        # remove the run
                        self.run_list.remove(run)

                        # continue processing
                        continue
                    # or an error
                    elif run['job-type'] == JobType.error:
                        # report the exception
                        self.logger.error(f"Error detected: About to clean up of intermediate files. Run id: {run['id']}")
                        run['status_prov'] += ', Error detected'
                        self.pg_db.update_job_status(run['id'], run['status_prov'])

                        # set the type to clean up
                        run['job-type'] = JobType.final_staging
                        run['status'] = JobStatus.new

                        # continue processing
                        continue

                except Exception as e_main:
                    # report the exception
                    self.logger.error(f"Cleanup exception detected, id: {run['id']}, \
                                        job-type: {run['job-type']}, status: {run['status_prov']}, downloadurl: {run['downloadurl']},\
                                        gridname: {run['gridname']}, instance_name: {run['instance_name']}, exception: {e_main}")

                    # continue processing runs
                    continue

                # catch handling the run exceptions
                try:
                    # handle the run
                    no_activity = self.handle_run(run)
                except Exception as e:
                    # report the exception
                    self.logger.error(f"Run handler exception detected, id: {run['id']}, \
                                        job-type: {run['job-type']}, status: {run['status_prov']}, downloadurl: {run['downloadurl']}, \
                                        gridname: {run['gridname']}, instance_name: {run['instance_name']}, exception: {e}")

                    # delete the k8s job if it exists
                    self.k8s_create.delete_job(run)

                    # prepare the DB status
                    run['status_prov'] += ', Run handler error detected'
                    self.pg_db.update_job_status(run['id'], run['status_prov'])

                    # set error conditions
                    run['job-type'] = JobType.error
                    run['status'] = JobStatus.error

                    # continue processing runs
                    continue

            # was there any activity
            if no_activity:
                # increment the counter
                no_activity_counter += 1
            else:
                no_activity_counter = 0

            # check for something to do after a period of time
            if no_activity_counter >= 10:
                # set the sleep timeout
                sleep_timeout = self.k8s_config['POLL_LONG_SLEEP']

                # try again at this poll rate
                no_activity_counter = 9
            else:
                # set the sleep timeout
                sleep_timeout = self.k8s_config['POLL_SHORT_SLEEP']

            self.logger.debug(f"All active run checks complete. Sleeping for {sleep_timeout / 60} minutes.")

            # wait longer for something to do
            time.sleep(sleep_timeout)

    def handle_run(self, run):
        """
        handles the run processing

        :param run:
        :return:
        """
        # init the activity flag
        no_activity: bool = False

        # is this a staging job
        if run['job-type'] == JobType.staging:
            # work the current state
            if run['status'] == JobStatus.new:
                # set the activity flag
                no_activity = False

                # get the data by the download url
                command_line_params = ['--inputURL', run['downloadurl'], '--outputDir']

                # create the job configuration for a new run
                self.k8s_create_job_obj(run, command_line_params, True)

                # execute the k8s job run
                self.k8s_create.execute(run)

                # set the current status
                run['status'] = JobStatus.staging_running
                run['status_prov'] += ', Staging running'
                self.pg_db.update_job_status(run['id'], run['status_prov'])

                self.logger.info(f"Job created. Run ID: {run['id']}, Job ID: {run[run['job-type']]['job-config']['job_id']}, Job type: {run['job-type']}")
            elif run['status'] == JobStatus.staging_running and run['status'] != JobStatus.error:
                # set the activity flag
                no_activity = False

                # find the job, get the status
                job_status, job_pod_status = self.k8s_find.find_job_info(run)

                # if the job status is not active (!=1) it is complete or dead. either way it gets removed
                if job_status is None and not job_pod_status.startswith('Failed'):
                    # remove the job and get the final run status
                    job_status = self.k8s_create.delete_job(run)

                    self.logger.info(f"Job complete. Run ID: {run['id']}, Job ID: {run[run['job-type']]['job-config']['job_id']}, Job type: {run['job-type']}, Final status: {job_status}")

                    # set the next stage and stage status
                    run['job-type'] = JobType.obs_mod
                    run['status'] = JobStatus.new
                    run['status_prov'] += ', Staging complete'
                    self.pg_db.update_job_status(run['id'], run['status_prov'])
                # was there a failure
                elif job_pod_status.startswith('Failed'):
                    # remove the job and get the final run status
                    job_status = self.k8s_create.delete_job(run)

                    self.logger.error(f"Error: Run status {run['status']}. Run ID: {run['id']}, Job type: {run['job-type']}, Job ID: {run[run['job-type']]['job-config']['job_id']}, job status: {job_status}, pod status: {job_pod_status}.")
                    self.send_slack_msg(run['id'], f"failed in {run['job-type']}.", run['instance_name'])

                    # set error conditions
                    run['job-type'] = JobType.error
                    run['status'] = JobStatus.error

        # is this a obs_mod job
        elif run['job-type'] == JobType.obs_mod:
            # work the current state
            if run['status'] == JobStatus.new:
                # set the activity flag
                no_activity = False

                # the download URL given is for a file server. obs/mod needs the dodsC access type
                access_type = run['downloadurl'] + '/fort.63.nc'
                access_type = access_type.replace('fileServer', 'dodsC')

                # create the additional command line parameters
                command_line_params = ['--instanceId', str(run['id']),
                                       '--inputURL', access_type, '--grid', run['gridname'],
                                       '--outputDIR',
                                       self.k8s_config[run['job-type']]['DATA_MOUNT_PATH'] + '/' +
                                       str(run['id']) +
                                       self.k8s_config[run['job-type']]['SUB_PATH'] +
                                       self.k8s_config[run['job-type']]['ADDITIONAL_PATH'],
                                       '--finalDIR', self.k8s_config[run['job-type']]['DATA_MOUNT_PATH'] + '/' +
                                       str(run['id']) + '/' +
                                       'final' + self.k8s_config[run['job-type']]['ADDITIONAL_PATH']
                                       ]

                # create the job configuration for a new run
                self.k8s_create_job_obj(run, command_line_params)

                # execute the k8s job run
                self.k8s_create.execute(run)

                # move to the next stage
                run['status'] = JobStatus.obs_mod_running
                run['status_prov'] += ', Obs/Mod running'
                self.pg_db.update_job_status(run['id'], run['status_prov'])

                self.logger.info(f"Job created. Run ID: {run['id']}, Job ID: {run[run['job-type']]['job-config']['job_id']}, Job type: {run['job-type']}")
            elif run['status'] == JobStatus.obs_mod_running and run['status'] != JobStatus.error:
                # set the activity flag
                no_activity = False

                # find the job, get the status
                job_status, job_pod_status = self.k8s_find.find_job_info(run)

                # if the job status is not active (No) it is complete or dead. either way it gets removed
                if job_status is None and not job_pod_status.startswith('Failed'):
                    # remove the job and get the final run status
                    job_status = self.k8s_create.delete_job(run)

                    self.logger.info(f"Job complete. Run ID: {run['id']}, Job ID: {run[run['job-type']]['job-config']['job_id']}, Job type: {run['job-type']}, Final job status: {job_status}")

                    # set the next stage and stage status
                    run['job-type'] = JobType.run_geo_tiff
                    run['status'] = JobStatus.new
                    run['status_prov'] += ', Obs/Mod complete'
                    self.pg_db.update_job_status(run['id'], run['status_prov'])
                # was there a failure
                elif job_pod_status.startswith('Failed'):
                    # remove the job and get the final run status
                    job_status = self.k8s_create.delete_job(run)

                    self.logger.error(f"Error: Run status {run['status']}. Run ID: {run['id']}, Job type: {run['job-type']}, Job ID: {run[run['job-type']]['job-config']['job_id']}, job status: {job_status}, pod status: {job_pod_status}.")
                    self.send_slack_msg(run['id'], f"failed in {run['job-type']}.", run['instance_name'])

                    # set error conditions
                    run['job-type'] = JobType.error
                    run['status'] = JobStatus.error

        # is this a geo tiff job array
        elif run['job-type'] == JobType.run_geo_tiff:
            # work the current state
            if run['status'] == JobStatus.new:
                # set the activity flag
                no_activity = False

                # create the additional command line parameters
                command_line_params = ['--outputDIR',
                                       self.k8s_config[run['job-type']]['DATA_MOUNT_PATH'] + '/' +
                                       str(run['id']) +
                                       self.k8s_config[run['job-type']]['SUB_PATH'],
                                       '--finalDIR',
                                       self.k8s_config[run['job-type']]['DATA_MOUNT_PATH'] + '/' +
                                       str(run['id']) + '/' +
                                       'final' + self.k8s_config[run['job-type']]['SUB_PATH'],
                                       '--inputFile']

                # create the job configuration for a new run
                self.k8s_create_job_obj(run, command_line_params)

                # execute the k8s job run
                self.k8s_create.execute(run)

                # move to the next stage
                run['status'] = JobStatus.run_geo_tiff_running
                run['status_prov'] += ', Geo tiff running'
                self.pg_db.update_job_status(run['id'], run['status_prov'])

                self.logger.info(f"Job created. Run ID: {run['id']}, Job ID: {run[run['job-type']]['job-config']['job_id']}, Job type: {run['job-type']}")
            elif run['status'] == JobStatus.run_geo_tiff_running and run['status'] != JobStatus.error:
                # set the activity flag
                no_activity = False

                # find the job, get the status
                job_status, job_pod_status = self.k8s_find.find_job_info(run)

                # if the job status is not active (!=1) it is complete or dead. either way it gets removed
                if job_status is None and not job_pod_status.startswith('Failed'):
                    # remove the job and get the final run status
                    job_status = self.k8s_create.delete_job(run)

                    self.logger.info(f"Job complete. Run ID: {run['id']}, Job ID: {run[run['job-type']]['job-config']['job_id']}, Job type: {run['job-type']}, Final job status: {job_status}")

                    # set the next stage and stage status
                    run['job-type'] = JobType.compute_mbtiles_0_9
                    run['status'] = JobStatus.new
                    run['status_prov'] += ', Geo tiff complete'
                    self.pg_db.update_job_status(run['id'], run['status_prov'])
                # was there a failure
                elif job_pod_status.startswith('Failed'):
                    # remove the job and get the final run status
                    job_status = self.k8s_create.delete_job(run)

                    self.logger.error(f"Error: Run status {run['status']}. Run ID: {run['id']}, Job type: {run['job-type']}, Job ID: {run[run['job-type']]['job-config']['job_id']}, job status: {job_status}, pod status: {job_pod_status}.")
                    self.send_slack_msg(run['id'], f"failed in {run['job-type']}.", run['instance_name'])

                    # set error conditions
                    run['job-type'] = JobType.error
                    run['status'] = JobStatus.error

        # is this a mbtiles zoom 0-9 job array
        elif run['job-type'] == JobType.compute_mbtiles_0_9:
            # work the current state
            if run['status'] == JobStatus.new:
                # set the activity flag
                no_activity = False

                # create the additional command line parameters
                command_line_params = ['--outputDIR',
                                       self.k8s_config[run['job-type']]['DATA_MOUNT_PATH'] + '/' +
                                       str(run['id']) + self.k8s_config[run['job-type']]['SUB_PATH'],
                                       '--finalDIR',
                                       self.k8s_config[run['job-type']]['DATA_MOUNT_PATH'] + '/' +
                                       str(run['id']) + '/' +
                                       'final' + self.k8s_config[run['job-type']]['SUB_PATH'],
                                       '--inputFile'
                                       ]

                # create the job configuration for a new run
                self.k8s_create_job_obj(run, command_line_params)

                # execute the k8s job run
                self.k8s_create.execute(run)

                # move to the next stage
                run['status'] = JobStatus.compute_mbtiles_0_9_running
                run['status_prov'] += ', Compute mbtiles 0-9 running'
                self.pg_db.update_job_status(run['id'], run['status_prov'])

                self.logger.info(f"Job created. Run ID: {run['id']}, Job ID: {run[run['job-type']]['job-config']['job_id']}, Job type: {run['job-type']}")
            elif run['status'] == JobStatus.compute_mbtiles_0_9_running and run['status'] != JobStatus.error:
                # set the activity flag
                no_activity = False

                # find the job, get the status
                job_status, job_pod_status = self.k8s_find.find_job_info(run)

                # if the job status is not active (!=1) it is complete or dead. either way it gets removed
                if job_status is None and not job_pod_status.startswith('Failed'):
                    # remove the job and get the final run status
                    job_status = self.k8s_create.delete_job(run)

                    self.logger.info(f"Job complete. Run ID: {run['id']}, Job ID: {run[run['job-type']]['job-config']['job_id']}, Job type: {run['job-type']}, Final job status: {job_status}")

                    # set the next stage and stage status
                    run['job-type'] = JobType.load_geo_server
                    run['status'] = JobStatus.new
                    run['status_prov'] += ', Compute mbtiles zoom 0-9 complete'
                    self.pg_db.update_job_status(run['id'], run['status_prov'])
                # was there a failure
                elif job_pod_status.startswith('Failed'):
                    # remove the job and get the final run status
                    job_status = self.k8s_create.delete_job(run)

                    self.logger.error(f"Error: Run status {run['status']}. Run ID: {run['id']}, Job type: {run['job-type']}, Job ID: {run[run['job-type']]['job-config']['job_id']}, job status: {job_status}, pod status: {job_pod_status}.")
                    self.send_slack_msg(run['id'], f"failed in {run['job-type']}.", run['instance_name'])

                    # set error conditions
                    run['job-type'] = JobType.error
                    run['status'] = JobStatus.error

        # is this a mbtiles part 1 job array
        elif run['job-type'] == JobType.compute_mbtiles_10:
            # work the current state
            if run['status'] == JobStatus.new:
                # set the activity flag
                no_activity = False

                # create the additional command line parameters
                command_line_params = ['--outputDIR', self.k8s_config[run['job-type']]['DATA_MOUNT_PATH'] + '/' +
                                       str(run['id']) + self.k8s_config[run['job-type']]['SUB_PATH'],
                                       '--finalDIR', self.k8s_config[run['job-type']]['DATA_MOUNT_PATH'] + '/' +
                                       str(run['id']) + '/' +
                                       'final' + self.k8s_config[run['job-type']]['SUB_PATH'],
                                       '--inputFile'
                                       ]

                # create the job configuration for a new run
                self.k8s_create_job_obj(run, command_line_params)

                # execute the k8s job run
                self.k8s_create.execute(run)

                # move to the next stage
                run['status'] = JobStatus.compute_mbtiles_10_running
                run['status_prov'] += ',Compute mbtiles running'
                self.pg_db.update_job_status(run['id'], run['status_prov'])

                self.logger.info(f"Job created. Run ID: {run['id']}, Job ID: {run[run['job-type']]['job-config']['job_id']}, Job type: {run['job-type']}")
            elif run['status'] == JobStatus.compute_mbtiles_10_running and run['status'] != JobStatus.error:
                # set the activity flag
                no_activity = False

                # find the job, get the status
                job_status, job_pod_status = self.k8s_find.find_job_info(run)

                # if the job status is not active (!=1) it is complete or dead. either way it gets removed
                if job_status is None and not job_pod_status.startswith('Failed'):
                    # remove the job and get the final run status
                    job_status = self.k8s_create.delete_job(run)

                    self.logger.info(f"Job complete. Run ID: {run['id']}, Job ID: {run[run['job-type']]['job-config']['job_id']}, Job type: {run['job-type']}, Final job status: {job_status}")

                    # set the next stage and stage status
                    run['job-type'] = JobType.load_geo_server
                    run['status'] = JobStatus.new
                    run['status_prov'] += ', Compute mbtiles zoom 10 complete'
                    self.pg_db.update_job_status(run['id'], run['status_prov'])
                # was there a failure
                elif job_pod_status.startswith('Failed'):
                    # remove the job and get the final run status
                    job_status = self.k8s_create.delete_job(run)

                    self.logger.error(f"Error: Run status {run['status']}. Run ID: {run['id']}, Job type: {run['job-type']}, Job ID: {run[run['job-type']]['job-config']['job_id']}, job status: {job_status}, pod status: {job_pod_status}.")
                    self.send_slack_msg(run['id'], f"failed in {run['job-type']}.", run['instance_name'])

                    # set error conditions
                    run['job-type'] = JobType.error
                    run['status'] = JobStatus.error

        # is this a mbtiles part 1 job array
        elif run['job-type'] == JobType.compute_mbtiles_11:
            # work the current state
            if run['status'] == JobStatus.new:
                # set the activity flag
                no_activity = False

                # create the additional command line parameters
                command_line_params = ['--outputDIR', self.k8s_config[run['job-type']]['DATA_MOUNT_PATH'] + '/' +
                                       str(run['id']) + self.k8s_config[run['job-type']]['SUB_PATH'],
                                       '--finalDIR', self.k8s_config[run['job-type']]['DATA_MOUNT_PATH'] + '/' +
                                       str(run['id']) + '/' +
                                       'final' + self.k8s_config[run['job-type']]['SUB_PATH'],
                                       '--inputFile'
                                       ]

                # create the job configuration for a new run
                self.k8s_create_job_obj(run, command_line_params)

                # execute the k8s job run
                self.k8s_create.execute(run)

                # move to the next stage
                run['status'] = JobStatus.compute_mbtiles_11_running
                run['status_prov'] += ',Compute mbtiles running'
                self.pg_db.update_job_status(run['id'], run['status_prov'])

                self.logger.info(f"Job created. Run ID: {run['id']}, Job ID: {run[run['job-type']]['job-config']['job_id']}, Job type: {run['job-type']}")
            elif run['status'] == JobStatus.compute_mbtiles_11_running and run['status'] != JobStatus.error:
                # set the activity flag
                no_activity = False

                # find the job, get the status
                job_status, job_pod_status = self.k8s_find.find_job_info(run)

                # if the job status is not active (!=1) it is complete or dead. either way it gets removed
                if job_status is None and not job_pod_status.startswith('Failed'):
                    # remove the job and get the final run status
                    job_status = self.k8s_create.delete_job(run)

                    self.logger.info(f"Job complete. Run ID: {run['id']}, Job ID: {run[run['job-type']]['job-config']['job_id']}, Job type: {run['job-type']}, Final job status: {job_status}")

                    # set the next stage and stage status
                    run['job-type'] = JobType.load_geo_server
                    run['status'] = JobStatus.new
                    run['status_prov'] += ', Compute mbtiles zoom 11 complete'
                    self.pg_db.update_job_status(run['id'], run['status_prov'])
                # was there a failure
                elif job_pod_status.startswith('Failed'):
                    # remove the job and get the final run status
                    job_status = self.k8s_create.delete_job(run)

                    self.logger.error(f"Error: Run status {run['status']}. Run ID: {run['id']}, Job type: {run['job-type']}, Job ID: {run[run['job-type']]['job-config']['job_id']}, job status: {job_status}, pod status: {job_pod_status}.")
                    self.send_slack_msg(run['id'], f"failed in {run['job-type']}.", run['instance_name'])

                    # set error conditions
                    run['job-type'] = JobType.error
                    run['status'] = JobStatus.error

        # is this a geo server load job
        elif run['job-type'] == JobType.load_geo_server:
            # work the current state
            if run['status'] == JobStatus.new:
                # set the activity flag
                no_activity = False

                # create the additional command line parameters
                command_line_params = ['--instanceId', str(run['id'])]

                # create the job configuration for a new run
                self.k8s_create_job_obj(run, command_line_params)

                # execute the k8s job run
                self.k8s_create.execute(run)

                # set the current status
                run['status'] = JobStatus.load_geo_server_running
                run['status_prov'] += ', Load geo server running'
                self.pg_db.update_job_status(run['id'], run['status_prov'])

                self.logger.info(f"Job created. Run ID: {run['id']}, Job ID: {run[run['job-type']]['job-config']['job_id']}, Job type: {run['job-type']}")
            elif run['status'] == JobStatus.load_geo_server_running and run['status'] != JobStatus.error:
                # set the activity flag
                no_activity = False

                # find the job, get the status
                job_status, job_pod_status = self.k8s_find.find_job_info(run)

                # if the job status is not active (!=1) it is complete or dead. either way it gets removed
                if job_status is None and not job_pod_status.startswith('Failed'):
                    # remove the job and get the final run status
                    job_status = self.k8s_create.delete_job(run)

                    self.logger.info(f"Job complete. Run ID: {run['id']}, Job ID: {run[run['job-type']]['job-config']['job_id']}, Job type: {run['job-type']}, Final job status: {job_status}")

                    # set the type to complete
                    run['job-type'] = JobType.final_staging
                    run['status'] = JobStatus.new
                    run['status_prov'] += ', Load geo server complete'
                    self.pg_db.update_job_status(run['id'], run['status_prov'])
                # was there a failure
                elif job_pod_status.startswith('Failed'):
                    # remove the job and get the final run status
                    job_status = self.k8s_create.delete_job(run)

                    self.logger.error(f"Error: Run status {run['status']}. Run ID: {run['id']}, Job type: {run['job-type']}, Job ID: {run[run['job-type']]['job-config']['job_id']}, job status: {job_status}, pod status: {job_pod_status}.")
                    self.send_slack_msg(run['id'], f"failed in {run['job-type']}.", run['instance_name'])

                    # set error conditions
                    run['job-type'] = JobType.error
                    run['status'] = JobStatus.error

        # is this a final staging job
        elif run['job-type'] == JobType.final_staging:
            # work the current state
            if run['status'] == JobStatus.new:
                # set the activity flag
                no_activity = False

                # create the additional command line parameters
                command_line_params = ['--inputDir',
                                       self.k8s_config[run['job-type']]['DATA_MOUNT_PATH'] + '/' +
                                       str(run['id']) +
                                       self.k8s_config[run['job-type']]['SUB_PATH'],
                                       '--outputDir',
                                       self.k8s_config[run['job-type']]['DATA_MOUNT_PATH'] +
                                       self.k8s_config[run['job-type']]['SUB_PATH'],
                                       '--tarMeta',
                                       str(run['id'])]

                # create the job configuration for a new run
                self.k8s_create_job_obj(run, command_line_params, False)

                # execute the k8s job run
                self.k8s_create.execute(run)

                # set the current status
                run['status'] = JobStatus.final_staging_running
                run['status_prov'] += ', Final staging running'
                self.pg_db.update_job_status(run['id'], run['status_prov'])

                self.logger.info(f"Job created. Run ID: {run['id']}, Job ID: {run[run['job-type']]['job-config']['job_id']}, Job type: {run['job-type']}")
            elif run['status'] == JobStatus.final_staging_running and run['status'] != JobStatus.error:
                # set the activity flag
                no_activity = False

                # find the job, get the status
                job_status, job_pod_status = self.k8s_find.find_job_info(run)

                # if the job status is not active (!=1) it is complete or dead. either way it gets removed
                if job_status is None and not job_pod_status.startswith('Failed'):
                    # remove the job and get the final run status
                    job_status = self.k8s_create.delete_job(run)

                    self.logger.info(f"Job complete. Run ID: {run['id']}, Job ID: {run[run['job-type']]['job-config']['job_id']}, Job type: {run['job-type']}, Final job status: {job_status}")

                    # set the next stage and stage status
                    run['job-type'] = JobType.complete
                    run['status'] = JobStatus.final_staging_complete
                    run['status_prov'] += ', Final staging complete'
                    self.pg_db.update_job_status(run['id'], run['status_prov'])
                # was there a failure
                elif job_pod_status.startswith('Failed'):
                    # remove the job and get the final run status
                    job_status = self.k8s_create.delete_job(run)

                    self.logger.error(f"Error: Run status {run['status']}. Run ID: {run['id']}, Job type: {run['job-type']}, Job ID: {run[run['job-type']]['job-config']['job_id']}, job status: {job_status}, pod status: {job_pod_status}.")
                    self.send_slack_msg(run['id'], f"failed in {run['job-type']}. *Warning: Intermediate files may not have been removed.*", run['instance_name'])

                    # set error conditions
                    run['job-type'] = JobType.complete
                    run['status'] = JobStatus.final_staging_complete

        # return to the caller
        return no_activity

    def k8s_create_job_obj(self, run: dict, command_line_params: list, extend_output_path: bool = False):
        """
        Creates the details for a job from the database

        :return:
        """
        # create a new configuration if tis is a new run
        if run['status'] == JobStatus.new:
            # get the config
            config = self.get_config()[run['job-type']]

            # load the config with the info from the config file
            config['JOB_NAME'] += str(run['id']).lower()
            config['DATA_VOLUME_NAME'] += str(run['id']).lower()
            config['SSH_VOLUME_NAME'] += str(run['id']).lower()
            config['COMMAND_LINE'].extend(command_line_params)

            # tack on any additional paths if requested
            if extend_output_path:
                config['SUB_PATH'] = '/' + str(run['id']) + config['SUB_PATH']
                config['COMMAND_LINE'].extend([config['DATA_MOUNT_PATH'] + config['SUB_PATH'] + config['ADDITIONAL_PATH']])

            # save these params in the run info
            run[run['job-type']] = {'run-config': config}

    def send_slack_msg(self, run_id, msg, instance_name=None):
        """
        sends a msg to the slack channel

        :param run_id:
        :param msg:
        :param instance_name:
        :return:
        """
        # init the final msg
        final_msg = "APSViz Supervisor - "

        # if there was an instance name use it
        if instance_name is not None:
            final_msg += f'Instance name: ' + instance_name + ', '

        # add the run id and msg
        final_msg += f'Run ID: {run_id} ' + msg

        # send the message
        self.slack_client.chat_postMessage(channel=self.slack_channel, text=final_msg)

    def get_incomplete_runs(self):
        """
        get the list of instances that need processing

        :return: nothing
        """
        # get the new runs
        runs = self.pg_db.get_new_runs()

        # did we find anything to do
        if runs != -1 and runs is not None:
            # add this run to the list
            for run in runs:
                # get the run id
                run_id = run['run_id']

                # continue only if we have everything needed for a run
                if 'downloadurl' in run['run_data'] and 'adcirc.gridname' in run['run_data'] and 'instancename' in run['run_data']:
                    # create the new run
                    self.run_list.append({'id': run_id, 'job-type': JobType.staging, 'status': JobStatus.new, 'status_prov': 'New, Run accepted', 'downloadurl': run['run_data']['downloadurl'], 'gridname': run['run_data']['adcirc.gridname'], 'instance_name': run['run_data']['instancename']})

                    # update the run status in the DB
                    self.pg_db.update_job_status(run_id, 'New, Run accepted')

                    # notify slack
                    self.send_slack_msg(run_id, "accepted.", run['run_data']['instancename'])
                else:
                    # update the run status in the DB
                    self.pg_db.update_job_status(run_id, 'Error - Lacks the required run properties.')

                    # if there was an instance name use it
                    if 'instancename' in run['run_data']:
                        instance_name = run['run_data']['instancename']
                    else:
                        instance_name = None

                    # send the slack message
                    self.send_slack_msg(run_id, "lacked the required run properties.", instance_name)

        # debugging only
        """
            SELECT id, key, value, instance_id FROM public."ASGS_Mon_config_item" where instance_id=2620;
            
            SELECT public.get_config_items_json(2620);
            SELECT public.get_supervisor_config_items_json();
            
            UPDATE public."ASGS_Mon_config_item" SET value='do not run' WHERE key='supervisor_job_status' AND instance_id=0;
            
            select * from public."ASGS_Mon_config_item" where instance_id=0 and uid='' and key in ('downloadurl','adcirc.gridname','supervisor_job_status', 'instancename');

            select pg_terminate_backend(pid) from pg_stat_activity where datname='adcirc_obs';

            select distinct id, instance_id, uid, key, value
                FROM public."ASGS_Mon_config_item"
                where key in ('supervisor_job_status')--, 'adcirc.gridname', 'downloadurl', 'instancename'
                and instance_id in (select id from public."ASGS_Mon_instance" order by id desc)
                --and uid='2021052506-namforecast'
                order by 2 desc, 1 desc, 4, 5;   
            
            select distinct
                'SELECT public.set_config_item(' || instance_id || ', ''' || uid || ''', ''supervisor_job_status'', ''new'');' as cmd
                FROM public."ASGS_Mon_config_item"
                where key in ('supervisor_job_status')--, 'adcirc.gridname', 'downloadurl', 'instancename'
                and instance_id in (select id from public."ASGS_Mon_instance" order by id desc)
                and value='New, Run accepted, Staging running, Staging complete, Obs/Mod running, Obs/Mod complete, Geo tiff running';
              
            --SELECT public.set_config_item(0, 'x', 'supervisor_job_status', 'new');	
        """
        # self.run_list.append({'id': 2620, 'job-type': JobType.staging, 'status': JobStatus.new, 'status_prov': 'New, Run accepted'})
        # self.run_list.append({'id': 2620, 'job-type': JobType.obs_mod, 'status': JobStatus.new, 'status_prov': 'New, Run accepted'})
        # self.run_list.append({'id': 2620, 'job-type': JobType.run_geo_tiff, 'status': JobStatus.new, 'status_prov': 'New, Run accepted'})
        # self.run_list.append({'id': 2620, 'job-type': JobType.compute_mbtiles_1, 'status': JobStatus.new, 'status_prov': 'New, Run accepted'})
