"""Manages all Cloud Run-specific aspects of the deployment process."""


import sys, os, re, subprocess
from pathlib import Path

from django.conf import settings
from django.core.management.base import CommandError
from django.core.management.utils import get_random_secret_key
from django.utils.crypto import get_random_string
from django.utils.safestring import mark_safe

from simple_deploy.management.commands import deploy_messages as d_msgs
from simple_deploy.management.commands.cloudrun import deploy_messages as cloudrun_msgs

from simple_deploy.management.commands.utils import write_file_from_template

# TODO(glasnt): consider if a variation of self.sd.project_name could be used
# (https://github.com/ehmatthes/django-simple-deploy/issues/193#issuecomment-1338646565)
CLOUD_RUN_SERVICE_NAME = "django"

# TODO(glasnt): use self.sd.region  
CLOUD_RUN_REGION = "us-central1"

class PlatformDeployer:
    """Perform the initial deployment of a simple project.
    Configure as much as possible automatically.
    """

    def __init__(self, command):
        """Establishes connection to existing simple_deploy command object."""
        self.sd = command
        self.stdout = self.sd.stdout


    def deploy(self, *args, **options):
        self.sd.write_output("Configuring project for deployment to Cloud Run...")

        self._get_googlecloud_project()
        self._create_placeholder()
        self._set_on_cloudrun()

        self._generate_procfile()
        self._add_gcloudignore()
        self._add_cloudbuild_yaml()
        self._modify_settings()

        self._add_gunicorn()
        self._add_psycopg2_binary()
        self._add_dj_database_url()
        self._add_whitenoise()

        self._conclude_automate_all()

        self._show_success_message()

    def _get_googlecloud_project(self):
        """Use the gcloud CLI to get the current active project"""
        msg = "Finding active Google Cloud project"

        cmd = f"gcloud config get-value project"
        output_obj = self.sd.execute_subp_run(cmd)
        project_id = output_obj.stdout.decode()
        if project_id == "(unset)": 
            raise CommandError(cloudrun_msgs.no_project_id)
        msg = f"  Found Google Cloud project: {project_id}"
        self.sd.write_output(msg)

    def _get_googlerun_region(self):
        """Use the gcloud CLI to get the Cloud Run region"""
        msg = "Finding configured Cloud Run region"

        cmd = f"gcloud config get-value run/region"
        output_obj = self.sd.execute_subp_run(cmd)
        region = output_obj.stdout.decode()
        if region == "(unset)": 
            raise CommandError(cloudrun_msgs.no_cloudrun_region)
        msg = f"  Found Cloud Run region: {region}"
        self.sd.write_output(msg)
        return region


    def _create_placeholder(self):
        """Within the context of Google Cloud, things can exist.
        But within the context of a Cloud Run service, the service has to exist
        because configurations can be made against it. 
        The image of the service doesn't matter, but the service has to exist first. 
        The way to do this is to create a service with an initial placeholder revision
        using the "hello" placeholder service. https://github.com/googlecloudplatform/cloud-run-hello
        """
        msg = "Creating placeholder service..."
        self.sd.write_output(msg)

        # First, check service doesn't already exist.
        cmd = f"gcloud run services describe {CLOUD_RUN_SERVICE_NAME}"
        output_obj = self.sd.execute_subp_run(cmd)
        return_code = output_obj.returncode
        if return_code == 0:
            msg = "  Found placeholder service"
            self.sd.write_output(msg)
            return
        
        # Create placeholder service
        # Set visibility at this stage, and it should be always publicly accessible.
        cmd = f"gcloud run deploy django --image gcr.io/cloudrun/hello --allow-unauthenticated"
        output_obj = self.sd.execute_subp_run(cmd)
        output_str = output_obj.stdout.decode()
        self.sd.write_output(output_str)

        msg = "  Placeholder service created."
        self.sd.write_output(msg)



    def _set_on_cloudrun(self):
        """Set an environment variable, ON_CLOUDRUN. This is used in settings.py to apply
        deployment-specific settings.
        Returns:
        - None
        """
        msg = "Setting ON_CLOUDRUN envvar..."
        self.sd.write_output(msg)

        # First check if envvar has already been set.
        cmd = f"""gcloud run services describe {CLOUD_RUN_SERVICE_NAME} \
                    --format \"value(spec.template.spec.containers[0].env)\""""
        output_obj = self.sd.execute_subp_run(cmd)
        output_str = output_obj.stdout.decode()
        if 'ON_CLOUDRUN' in output_str:
            msg = "  Found ON_CLOUDRUN in existing envvars."
            self.sd.write_output(msg)
            return

        cmd = f"""gcloud run services update {CLOUD_RUN_SERVICE_NAME} \
                --set-env-vars ON_CLOUDRUN=1"""
        output_obj = self.sd.execute_subp_run(cmd)
        output_str = output_obj.stdout.decode()
        self.sd.write_output(output_str)

        msg = "  Set ON_CLOUDRUN envvar."
        self.sd.write_output(msg)


    def _generate_procfile(self):
        """Create Procfile, if none present."""

        #   Procfile should be in project root, if present.
        self.sd.write_output(f"\n  Looking in {self.sd.git_path} for Procfile...")
        procfile_present = 'Procfile' in os.listdir(self.sd.git_path)

        if procfile_present:
            self.sd.write_output("    Found existing Procfile.")
        else:
            self.sd.write_output("    No Procfile found. Generating Procfile...")
            if self.sd.nested_project:
                proc_command = f"web: gunicorn {self.sd.project_name}.{self.sd.project_name}.wsgi --log-file -"
            else:
                proc_command = f"web: gunicorn {self.sd.project_name}.wsgi --log-file -"

            migrate_command = "migrate: python manage.py migrate && python manage.py collectstatic --noinput"

            with open(f"{self.sd.git_path}/Procfile", 'w') as f:
                f.write(proc_command)
                f.write(migrate_command)

            self.sd.write_output("    Generated Procfile with following process:")
            self.sd.write_output(f"      {proc_command}")
            self.sd.write_output(f"      {migrate_command}")
            


    def _add_gcloudignore(self):
        """Add a gcloudignore file, based on user's local project environmnet.
        Ignore virtual environment dir, system-specific cruft, and IDE cruft.

        Based on the dockerignore config from flyio

        If an existing gcloudignore is found, make note of that but don't overwrite.

        Returns:
        - True if added gcloudignore.
        - False if gcloudignore found unnecessary, or if an existing dockerfile
          was found.
        """

        # Check for existing gcloudignore file; we're only looking in project root.
        #   If we find one, don't make any changes.
        path = Path('.gcloudignore')
        if path.exists():
            msg = "  Found existing .gcloudignore file. Not overwriting this file."
            self.sd.write_output(msg)
            return

        # Build gcloudignore string.
        gcloudignore_str = ""

        # Ignore git repository.
        gcloudignore_str += ".git/\n"

        # Ignore venv dir if a venv is active.
        venv_dir = os.environ.get("VIRTUAL_ENV")
        if venv_dir:
            venv_path = Path(venv_dir)
            gcloudignore_str += f"\n{venv_path.name}/\n"


        # Add python cruft.
        gcloudignore_str += "\n__pycache__/\n*.pyc\n"

        # Ignore any SQLite databases.
        gcloudignore_str += "\n*.sqlite3\n"

        # If on macOS, add .DS_Store.
        if self.sd.on_macos:
            gcloudignore_str += "\n.DS_Store\n"

        # Write file.
        path.write_text(gcloudignore_str)
        msg = "  Wrote .gcloudignore file."
        self.sd.write_output(msg)


    def _add_flytoml_file(self):
        """Add a minimal fly.toml file."""
        # File should be in project root, if present.
        self.sd.write_output(f"\n  Looking in {self.sd.git_path} for fly.toml file...")
        flytoml_present = 'fly.toml' in os.listdir(self.sd.git_path)

        if flytoml_present:
            self.sd.write_output("    Found existing fly.toml file.")
        else:
            # Generate file from template.
            context = {
                'deployed_project_name': self.deployed_project_name, 
                }
            path = self.sd.project_root / 'fly.toml'
            write_file_from_template(path, 'fly.toml', context)

            msg = f"\n    Generated fly.toml: {path}"
            self.sd.write_output(msg)
            return path


    def _modify_settings(self):
        """Add settings specific to Fly.io."""
        #   Check if a fly.io section is present. If not, add settings. If already present,
        #   do nothing.
        self.sd.write_output("\n  Checking if settings block for Fly.io present in settings.py...")

        with open(self.sd.settings_path) as f:
            settings_string = f.read()

        if 'if os.environ.get("ON_FLYIO"):' in settings_string:
            self.sd.write_output("\n    Found Fly.io settings block in settings.py.")
            return

        # Add Fly.io settings block.
        self.sd.write_output("    No Fly.io settings found in settings.py; adding settings...")

        safe_settings_string = mark_safe(settings_string)
        context = {
            'current_settings': safe_settings_string,
            'deployed_project_name': self.deployed_project_name,
        }
        path = Path(self.sd.settings_path)
        write_file_from_template(path, 'settings.py', context)

        msg = f"    Modified settings.py file: {path}"
        self.sd.write_output(msg)


    def _add_gunicorn(self):
        """Add gunicorn to project requirements."""
        self.sd.write_output("\n  Looking for gunicorn...")

        if self.sd.using_req_txt:
            self.sd.add_req_txt_pkg('gunicorn')
        elif self.sd.using_pipenv:
            self.sd.add_pipenv_pkg('gunicorn')


    def _add_psycopg2_binary(self):
        """Add psycopg2-binary to project requirements."""
        self.sd.write_output("\n  Looking for psycopg2-binary...")

        if self.sd.using_req_txt:
            self.sd.add_req_txt_pkg('psycopg2-binary')
        elif self.sd.using_pipenv:
            self.sd.add_pipenv_pkg('psycopg2-binary')

    def _add_dj_database_url(self):
        """Add dj-database-url to project requirements."""
        self.sd.write_output("\n  Looking for dj-database-url...")

        if self.sd.using_req_txt:
            self.sd.add_req_txt_pkg('dj-database-url')
        elif self.sd.using_pipenv:
            self.sd.add_pipenv_pkg('dj-database-url')

    def _add_whitenoise(self):
        """Add whitenoise to project requirements."""
        self.sd.write_output("\n  Looking for whitenoise...")

        if self.sd.using_req_txt:
            self.sd.add_req_txt_pkg('whitenoise')
        elif self.sd.using_pipenv:
            self.sd.add_pipenv_pkg('whitenoise')


    def _conclude_automate_all(self):
        """Finish automating the push to Fly.io.
        - Commit all changes.
        - Call `fly deploy`.
        - Call `fly open`, and grab URL.
        """
        # Making this check here lets deploy() be cleaner.
        if not self.sd.automate_all:
            return

        self.sd.commit_changes()

        # Push project.
        # Use execute_command() to stream output of this long-running command.
        self.sd.write_output("  Deploying to Fly.io...")
        cmd = "fly deploy"
        self.sd.execute_command(cmd)

        # Open project.
        self.sd.write_output("  Opening deployed app in a new browser tab...")
        cmd = "fly open"
        output = self.sd.execute_subp_run(cmd)
        self.sd.write_output(output)

        # Get url of deployed project.
        url_re = r'(opening )(http.*?)( \.\.\.)'
        output_str = output.stdout.decode()
        m = re.search(url_re, output_str)
        if m:
            self.deployed_url = m.group(2).strip()


    def _show_success_message(self):
        """After a successful run, show a message about what to do next."""

        # DEV:
        # - Mention that this script should not need to be run again, unless
        #   creating a new deployment.
        #   - Describe ongoing approach of commit, push, migrate. Lots to consider
        #     when doing this on production app with users, make sure you learn.

        if self.sd.automate_all:
            msg = cloudrun_msgs.success_msg_automate_all(self.deployed_url)
            self.sd.write_output(msg)
        else:
            msg = cloudrun_msgs.success_msg(log_output=self.sd.log_output)
            self.sd.write_output(msg)


    # --- Methods called from simple_deploy.py ---

    def confirm_preliminary(self):
        """Deployment to Fly.io is in a preliminary state, and we need to be
        explicit about that.
        """
        # Skip this confirmation when unit testing.
        if self.sd.unit_testing:
            return

        self.stdout.write(cloudrun_msgs.confirm_preliminary)
        confirmed = self.sd.get_confirmation(skip_logging=True)

        if confirmed:
            self.stdout.write("  Continuing with Cloud Run deployment...")
        else:
            # Quit and invite the user to try another platform.
            # We are happily exiting the script; there's no need to raise a
            #   CommandError.
            self.stdout.write(cloudrun_msgs.cancel_cloudrun)
            sys.exit()


    def validate_platform(self):
        """Make sure the local environment and project supports deployment to
        Cloud Run.
        
        The returncode for a successful command is 0, so anything truthy means
          a command errored out.
        """
        self._validate_cli()

        # When running unit tests, will not be logged into CLI.
        if not self.sd.unit_testing:
            self.deployed_project_name = self._get_deployed_project_name()

            # If using automate_all, we need to create the app before creating
            #   the db. But if there's already an app with no deployment, we can 
            #   use that one (maybe created from a previous automate_all run).
            # DEV: Update _get_deployed_project_name() to not throw error if
            #   using automate_all. _create_flyio_app() can exit if not using
            #   automate_all(). If self.deployed_project_name is set, just return
            #   because we'll use that project. If it's not set, call create.
            if not self.deployed_project_name and self.sd.automate_all:
                self.deployed_project_name = self._create_flyio_app()

            # Create the db now, before any additional configuration. Get region
            #   so we know where to create the db.
            self.region = self._get_region()
            self._create_db()
        else:
            self.deployed_project_name = self.sd.deployed_project_name


    def prep_automate_all(self):
        """Take any further actions needed if using automate_all."""
        # All creation has been taken earlier, during validation.
        pass


    # --- Helper methods for methods called from simple_deploy.py ---

    def _validate_cli(self):
        """Make sure the Google Cloud CLI is installed."""
        cmd = 'gcloud version'
        output_obj = self.sd.execute_subp_run(cmd)
        if output_obj.returncode:
            raise CommandError(cloudrun_msgs.cli_not_installed)

    def _get_deployed_project_name(self):
        """Get the Fly.io project name.
        Parse the output of `flyctl apps list`, and look for an app name
          that doesn't have a value set for LATEST DEPLOY. This indicates
          an app that has just been created, and has not yet been deployed.

        Returns:
        - String representing deployed project name.
        - Empty string if no deployed project name found, but using automate_all.
        - Raises CommandError if deployed project name can't be found.
        """
        msg = "\nLooking for Fly.io app to deploy against..."
        self.sd.write_output(msg, skip_logging=True)

        # Get apps info.
        cmd = "flyctl apps list"
        output_obj = self.sd.execute_subp_run(cmd)
        output_str = output_obj.stdout.decode()

        # Only keep relevant output; get rid of blank lines, update messages,
        #   and line with labels like NAME and LATEST DEPLOY.
        lines = output_str.split('\n')
        lines = [line for line in lines if line]
        lines = [line for line in lines if 'update' not in line.lower()]
        lines = [line for line in lines if 'NAME' not in line]
        lines = [line for line in lines if 'builder' not in line]

        # An app that has not been deployed to will only have values set for NAME,
        #   OWNER, and STATUS. PLATFORM and LATEST DEPLOY will be empty.
        app_name = ''
        for line in lines:
            # The desired line has three elements.
            parts = line.split()
            if len(parts) == 3:
                app_name = parts[0]

        # Return deployed app name, or raise CommandError.
        if app_name:
            msg = f"  Found Fly.io app: {app_name}"
            self.sd.write_output(msg, skip_logging=True)
            return app_name
        elif self.sd.automate_all:
            msg = "  No app found, but continuing with --automate-all..."
            self.sd.write_output(msg, skip_logging=True)
            # Simply return an empty string to indicate no suitable app was found,
            #   and we'll create one later.
            return ""
        else:
            # Can't continue without a Fly.io app to configure against.
            raise CommandError(flyio_msgs.no_project_name)

    def _create_flyio_app(self):
        """Create a new Fly.io app.
        Assumes caller already checked for automate_all, and that a suitable
          app is not already available.
        Returns:
        - String representing new app name.
        - Raises CommandError if an app can't be created.
        """
        msg = "  Creating a new app on Fly.io..."
        self.sd.write_output(msg, skip_logging=True)

        cmd = "flyctl apps create --generate-name"
        output_obj = self.sd.execute_subp_run(cmd)
        output_str = output_obj.stdout.decode()
        self.sd.write_output(output_str, skip_logging=True)

        # Get app name.
        app_name_re = r'(New app created: )(\w+\-\w+\-\d+)'
        flyio_app_name = ''
        m = re.search(app_name_re, output_str)
        if m:
            flyio_app_name = m.group(2).strip()

        if flyio_app_name:
            msg = f"  Created new app: {flyio_app_name}"
            self.sd.write_output(msg, skip_logging=True)
            return flyio_app_name
        else:
            # Can't continue without a Fly.io app to deploy to.
            raise CommandError(flyio_msgs.create_app_failed)


    def _get_region(self):
        """Get the region that the Fly.io app is configured for. We'll need this
        to create a postgres database.

        Parse the output of `flyctl regions list -a app_name`.

        Returns:
        - String representing region.
        - Raises CommandError if can't find region.
        """

        msg = "Looking for Fly.io region..."
        self.sd.write_output(msg, skip_logging=True)

        # Get region output.
        cmd = f"flyctl regions list -a {self.deployed_project_name}"
        output_obj = self.sd.execute_subp_run(cmd)
        output_str = output_obj.stdout.decode()

        # Look for first three-letter line after Region Pool.
        region = ''
        pool_found = False
        lines = output_str.split('\n')
        for line in lines:
            if not pool_found and "Region Pool" in line:
                pool_found = True
                continue

            # This is the first line after Region Pool.
            if pool_found:
                region = line.strip()
                break

        # Return region name, or raise CommandError.
        if region:
            msg = f"  Found region: {region}"
            self.sd.write_output(msg, skip_logging=True)
            return region
        else:
            # Can't continue without a Fly.io region to configure against.
            raise CommandError(flyio_msgs.region_not_found(self.deployed_project_name))

    def _create_db(self):
        """Create a db to deploy to, if none exists.
        Returns: 
        - 
        - Raises CommandError if...
        """
        msg = "Looking for a Postgres database..."
        self.sd.write_output(msg, skip_logging=True)

        db_exists = self._check_if_db_exists()

        if db_exists:
            return

        # No db found, create a new db.
        msg = f"  Create a new Postgres database..."
        self.sd.write_output(msg, skip_logging=True)

        self.db_name = f"{self.deployed_project_name}-db"
        cmd = f"flyctl postgres create --name {self.db_name} --region {self.region}"
        cmd += " --initial-cluster-size 1 --vm-size shared-cpu-1x --volume-size 1"

        # If not using automate_all, make sure it's okay to create a resource
        #   on user's account.
        if not self.sd.automate_all:
            self._confirm_create_db(db_cmd=cmd)

        # Create database.
        # Use execute_command(), to stream output of long-running process.
        self.sd.execute_command(cmd, skip_logging=True)

        msg = "  Created Postgres database."
        self.sd.write_output(msg, skip_logging=True)

        # Run `attach` command (and confirm DATABASE_URL is set?)
        msg = "  Attaching database to Fly.io app..."
        self.sd.write_output(msg, skip_logging=True)
        cmd = f"flyctl postgres attach --app {self.deployed_project_name} {self.db_name}"

        output_obj = self.sd.execute_subp_run(cmd)
        output_str = output_obj.stdout.decode()
        self.sd.write_output(output_str, skip_logging=True)

        msg = "  Attached database to app."
        self.sd.write_output(msg, skip_logging=True)

    def _check_if_db_exists(self):
        """Check if a postgres db already exists that should be used with this app.
        Returns:
        - True if db found.
        - False if not found.
        """

        # First, see if any Postgres clusters exist.
        cmd = "flyctl postgres list"
        output_obj = self.sd.execute_subp_run(cmd)
        output_str = output_obj.stdout.decode()

        if "No postgres clusters found" in output_str:
            msg = "  No Postgres database found."
            self.sd.write_output(msg, skip_logging=True)
            return False
        else:
            msg = "  A Postgres database was found."
            self.sd.write_output(msg, skip_logging=True)
            return True

    def _confirm_create_db(self, db_cmd):
        """We really need to confirm that the user wants a db created on their behalf.
        Show the command that will be run on the user's behalf.
        Returns:
        - True if confirmed.
        - Raises CommandError if not confirmed.
        """
        if self.sd.unit_testing:
            return

        self.stdout.write(flyio_msgs.confirm_create_db(db_cmd))
        confirmed = self.sd.get_confirmation(skip_logging=True)

        if confirmed:
            self.stdout.write("  Creating database...")
        else:
            # Quit and invite the user to create a database manually.
            raise CommandError(flyio_msgs.cancel_no_db)