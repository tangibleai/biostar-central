import logging
import hjson
import os
import io
from urllib.parse import urljoin
import requests
import subprocess
import sys
from functools import partial

from django.utils.encoding import force_text
from django.template import Template, Context
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.shortcuts import reverse
from django.utils import timezone

from biostar.emailer.auth import notify
from biostar.engine.models import Analysis, Project, Data, Job
from biostar.engine.api import change_image, get_thumbnail
from biostar.engine import auth
from biostar.accounts.models import User

logger = logging.getLogger('engine')

# Override the logger.
logger.setLevel(logging.INFO)


class Bunch():
    def __init__(self, **kwargs):
        self.value = ''
        self.name = self.summary = ''
        self.help = self.type = self.link = ''
        self.__dict__.update(kwargs)


def build_api_url(root_url, uid=None, view="recipe_api_list", api_key=None):

    url = reverse(view, kwargs=dict(uid=uid)) if uid else reverse(view)
    #TODO Put together params in diffrent way
    full_url = urljoin(root_url, url) + f"?k={api_key}"
    return full_url


def get_json_text(source, target_file=""):

    # All of source will be written to ( override ) target

    # Only replace name and help in target with items in source.
    if os.path.exists(target_file):
        target = hjson.loads(open(target_file, "r").read())
    else:
        target = {}

    for key in source:
        target[key] = source[key]

    return hjson.dumps(target)


def remote_upload(stream, root_url, uid, api_key, view):
    """
    Upload data found in stream to root_url.
    Currently uses PUT requests
    """

    payload = dict(k=api_key)
    # Build api url then send PUT request.
    full_url = build_api_url(root_url=root_url, view=view, uid=uid, api_key=api_key)
    response = requests.put(url=full_url, files=dict(file=stream), data=payload)
    if response.status_code == 404:
        logger.error(f"*** Object id : {uid} does not exist on remote host.")
        sys.exit()

    return response


def remote_download(root_url, api_key, view, uid, is_image, outfile, is_json):
    """
    Download data found in root_url using GET request.
    """
    mode = "wb" if is_image else "w"
    # Get data from the api url
    fullurl = build_api_url(root_url=root_url, view=view, uid=uid, api_key=api_key)
    response = requests.get(url=fullurl, params=dict(k=api_key))
    data = response.content if response.status_code == 200 else b""
    # Leave data encoded if its an image
    data = data if is_image else data.decode()
    # Format data and write to outfile.
    if is_json:
        data = get_json_text(source=hjson.loads(data), target_file=outfile)

    open(outfile, mode).write(data)

    return


def data_from_json(root, json_data, pid):
    project = Project.objects.get_all(uid=pid).first()

    # The data field is empty.
    if not json_data:
        logger.error(f"JSON file does not have a valid data field")
        return

    # The datalist is built from the json.
    data_list = [Bunch(**row) for row in json_data]

    # Add each collected datatype.
    for bunch in reversed(data_list):
        # This the path to the data.
        path = bunch.value

        # Makes the path relative if necessary.
        path = path if path.startswith("/") else os.path.join(root, path)

        # Create the data.
        auth.create_data(project=project, path=path, type=bunch.type,
                         name=bunch.name, text=bunch.help)


def load_db(uid, stream, pid=None, is_json=False, load_recipe=False, jobs=False, privacy=Project.PRIVATE):
    """
    Load "stream" into database object "uid".
    Loads object as a project by default.
    """
    def project():
        project = Project.objects.get_all(uid=uid).first()
        if not project:
            # Create empty object if not present and populate.
            # Select project owner.
            user = User.objects.filter(is_staff=True).first()
            project = auth.create_project(user=user, name="Project Name", uid=uid, privacy=privacy)
        conf = hjson.loads(stream.read())
        name = conf.get("settings", {}).get("name", project.name)
        text = conf.get("settings", {}).get("help", project.text)
        Project.objects.get_all(uid=uid).update(name=name, text=text)

        return project

    def recipe():
        recipe = Analysis.objects.get_all(uid=uid).first()
        project = Project.objects.get_all(uid=pid).first()
        if not recipe:
            # Create empty object if not present then populate.
            if not project:
                logger.error(f"*** Project id:{pid} does not exist.")
                sys.exit()
            recipe = auth.create_analysis(project=project, json_text="", template="", uid=uid, name="Recipe Name")

        if is_json:
            data = hjson.loads(stream.read())
            name = data.get("settings", {}).get("name", recipe.name)
            text = data.get("settings", {}).get("help", recipe.text)
            Analysis.objects.get_all(uid=uid).update(json_text=hjson.dumps(data), name=name, text=text)
        else:
            Analysis.objects.get_all(uid=uid).update(template=stream.read())

        if jobs:
            # When creating a job automatically for data in projects
            # it will try to match the value of the parameter to the data name.
            missing_name = ''
            for key, obj in recipe.json_data.items():
                if obj.get("source") != "PROJECT":
                    continue
                name = obj.get('value', '')
                data = Data.objects.filter(project=project, name=name).first()
                if not data:
                    missing_name = name
                    break
                data.fill_dict(obj)

            if missing_name:
                logger.error(f"Job not created! Missing data:{missing_name} in analysis:{recipe.name}")
            else:
                auth.create_job(analysis=recipe, json_data=recipe.json_data)
        return recipe

    return recipe() if load_recipe else project()


def upload(uid, root_dir, pid=None, root_url=None, api_key="", view="recipe_api_template", fname="",
           is_image=False, load_recipe=False, is_json=False, privacy=Project.PRIVATE, jobs=False):

    """
    Upload data into a remote host using API.
    Defaults to local database if root_url is None.
    """

    target = os.path.abspath(os.path.join(root_dir, fname))
    mode = "rb" if is_image else "r"
    if not os.path.exists(target):
        stream = open(get_thumbnail(), mode) if is_image else io.StringIO("")
    else:
        stream = open(target, mode)
    # Upload to remote host when url is set.
    if root_url:
        return remote_upload(stream=stream, root_url=root_url, uid=uid, api_key=api_key, view=view)
    # Update database info
    if is_image:
        # Update image file.
        mtype = Analysis if load_recipe else Project
        obj = mtype.objects.get_all(uid=uid).first()
        return change_image(obj=obj, file_object=stream)

    return load_db(uid=uid, pid=pid, stream=stream, is_json=is_json, load_recipe=load_recipe, privacy=privacy,
                   jobs=jobs)


def get_data_placeholder(is_json, is_image, uid):

    if is_image:
        placeholder = open(get_thumbnail(), "rb").read()
    elif is_json:
        data = dict(settings=dict(uid=uid,
                                  name="Object Name",
                                  image=f"{uid}.png",
                                  help="Help Text"))
        placeholder = hjson.dumps(data)
    else:
        placeholder = ""

    return placeholder


def download(uid, root_dir, root_url=None, api_key="", is_json=False, view="recipe_api_template",
             fname="", is_image=False, mtype=Analysis):

    # Get placeholder in case object has no image.
    img_path = lambda o: o.image.path if o.image else get_thumbnail()
    mode = "wb" if is_image else "w"
    # Make output directory.
    os.makedirs(root_dir, exist_ok=True)
    outfile = os.path.join(root_dir, fname)

    if root_url:
        remote_download(root_url=root_url, api_key=api_key, view=view, uid=uid,
                        is_image=is_image, outfile=outfile, is_json=is_json)
        return
    # Get data from database
    obj = mtype.objects.get_all(uid=uid).first()

    if not obj:
        data = get_data_placeholder(is_json=is_json, is_image=is_image, uid=uid)
        open(outfile, mode).write(data)
        return

    if is_image:
        data = open(img_path(obj), "rb").read()
    elif is_json:
        data = get_json_text(source=obj.json_data, target_file=outfile)
    else:
        data = obj.template

    open(outfile, mode).write(data)
    return outfile


def get_recipes_list(pid, root_url=None, api_key="", rid=""):
    """
    Return recipes belonging to project 'pid' from api if 'root_url' is given
    else return from database.
    """

    if root_url:
        # Get the recipes from remote url.
        recipe_api = build_api_url(root_url=root_url, api_key=api_key, uid=pid)
        data = requests.get(url=recipe_api, params=dict(k=api_key)).content
        data = data.decode("utf-8").split("\n")
        recipes = [r.split("\t")[0] for r in data if r]
        # Filter recipes from remote host.

        recipes = list(filter(lambda r: r == rid, recipes)) if rid else recipes
        return recipes
    query = Q(uid=rid) if rid else Q(project__uid=pid)
    recipes = Analysis.objects.get_all().filter(query)
    if recipes:
        recipes = recipes.values_list("uid", flat=True)
    else:
        # Allows for the creation of 'rid' if it doesn't exist.
        recipes = [rid] if rid else []
    return recipes


def get_conf(uid=None, root_url=None, api_key="", view="recipe_api_json",
                   mtype=Analysis, json_file=None):

    # Get json from url
    if root_url:
        fullurl = build_api_url(root_url=root_url, view=view, uid=uid, api_key=api_key)
        response = requests.get(url=fullurl, params=dict(k=api_key))
        json_text = response.text if response.status_code == 200 else ""
    # Get json from a file
    elif json_file:
        json_text = open(json_file).read() if os.path.exists(json_file) else ""
    # Get json from database
    else:
        obj = mtype.objects.get_all(uid=uid).first()
        json_text = hjson.dumps(obj.json_data) if obj else ""

    conf = hjson.loads(json_text).get("settings", {})
    return conf


def fname(conf, k=None, ext=".txt"):
    item = conf.get(k) if k else None
    placeholder = f"{'_'.join(conf.get('name', 'name').split())}-{conf.get('id')}"
    filename = item or placeholder + ext
    return filename


def get_json_files(root_dir, json_fname=None):
    """

    Return all .hjson or .json files in a directory
    """
    # All .hjson and json files in directory assumed to be recipes
    is_json = lambda p: p.endswith(".hjson") or p.endswith(".json")
    recipe_jsons = [recipe.name for recipe in os.scandir(root_dir) if recipe.is_file()
                    and is_json(recipe.name)]
    if json_fname:
        # Filter for one json file if provided.
        recipe_jsons = [ os.path.abspath(os.path.join(root_dir, json_fname)) ]

    return recipe_jsons


def recipe_loader(root_dir,  pid=None, json_fname=None, api_key="", root_url=None, rid="", url_from_json=False,
                  jobs=False, loaded=0):
    """
        Load recipes into api/database from a project found in project_dir.
        Uses PUT request so 'api_key' is required with 'root_url'.
    """
    if not os.path.exists(root_dir):
        logger.error(f"*** Directory: {root_dir} does not exist.")
        sys.exit()

    # All .hjson and json files in directory assumed to be recipes
    json_files = get_json_files(root_dir=root_dir, json_fname=json_fname)

    # Prepare the main function used to load.
    load = partial(upload, root_dir=root_dir, api_key=api_key, load_recipe=True)

    for json_file in json_files:
        source = os.path.abspath(os.path.join(root_dir, json_file))
        json_data = hjson.loads(open(source, "r").read())
        conf = json_data.get("settings", {})
        # Get uid from json
        uid = conf.get("uid")
        proj_uid = conf.get("project_uid")
        url = conf.get("url") if url_from_json else root_url

        # Skip loading when rid/pid provided does not equal target uid/pid
        if (rid and uid != rid) or (pid and proj_uid != pid):
            continue

        load(uid=uid, pid=proj_uid, root_url=url, fname=os.path.basename(json_file), view="recipe_api_json",
             is_json=True)
        load(uid=uid, pid=proj_uid, root_url=url, fname=fname(conf=conf, k="image", ext=".png"), view="recipe_api_image",
             is_image=True)
        # Start a job once the template has been loaded from remote host
        load(uid=uid, pid=proj_uid, root_url=url, fname=fname(conf=conf, k="template", ext=".sh"), jobs=jobs)
        loaded += 1
        print(f"*** Loaded recipe id: {uid}")

    return loaded


def recipe_dumper(root_dir, pid, root_url=None, api_key="", rid=""):
    """
    Dump recipes from the api/database into a target directory
    belonging to single project.
    """
    # Get the recipes uid list from API or database.
    recipes = get_recipes_list(pid=pid, root_url=root_url, api_key=api_key, rid=rid)
    dump = partial(download, root_url=root_url, root_dir=root_dir, api_key=api_key)

    for recipe_uid in recipes:
        conf = get_conf(uid=recipe_uid, root_url=root_url)
        # Dump json, template, and image with a request to each.
        dump(uid=recipe_uid, fname=fname(conf=conf, ext=".hjson"), is_json=True, view="recipe_api_json")
        dump(uid=recipe_uid, fname=fname(conf=conf, ext=".png"), is_image=True, view="recipe_api_image")
        dump(uid=recipe_uid, fname=fname(conf=conf, ext=".sh"))

        print(f"*** Dumped recipe id: {recipe_uid}")

    return recipes


def project_loader(root_dir, json_file=None, pid=None, root_url=None, api_key="", data=False, data_root="",
                   url_from_json=False, loaded=0):
    """
    Load projects from root_dir into remote host or local database
    """
    pmap = {"private": Project.PRIVATE, "public": Project.PUBLIC}
    json_files = get_json_files(root_dir=root_dir, json_fname=json_file)
    # Prepare function used to upload
    load = partial(upload, root_dir=root_dir, api_key=api_key)

    for project_json in json_files:
        source = os.path.abspath(os.path.join(root_dir, project_json))
        json_data = hjson.load(open(source, "r"))
        conf = json_data.get("settings", {})
        url = conf.get("url") if url_from_json else root_url
        uid = conf.get("uid") or pid
        privacy = conf.get("privacy", "").lower() or "private"
        privacy = pmap.get(privacy, Project.PRIVATE)
        # Skip anything that doesn't equal given pid.
        if pid and pid != uid:
            continue

        load(uid=uid, privacy=privacy, root_url=url, view="project_api_info", fname=source)
        load(uid=uid, root_url=url, is_image=True, view="project_api_image", fname=fname(conf=conf, k="image", ext=".png"))

        if data:
            add_data = json_data.get("data", [])
            data_from_json(root=data_root, pid=uid, json_data=add_data)
        loaded += 1
        print(f"*** Loaded project ({uid}).")
    return loaded


def project_dumper(pid, root_dir, root_url=None, api_key=""):
    """
    Dump project from remote host or local database into root_dir
    """

    # Prepare function used to download info and images
    dump = partial(download, mtype=Project, uid=pid, root_dir=root_dir, root_url=root_url, api_key=api_key)
    conf = get_conf(uid=pid, mtype=Project, root_url=root_url, view="project_api_info")
    # Get image name from json on remote host or database
    img_name = fname(conf=conf, ext=".png")
    json_file = fname(conf=conf, ext=".hjson")

    # Dump the project json and image
    dump(fname=json_file, view="project_api_info", is_json=True)
    dump(fname=img_name, view="project_api_image", is_image=True)

    print(f"*** Dumped project {pid}: {root_dir}.")
    return


def data_loader(path, pid=None, uid=None, update_toc=False, name="Data Name", type="", text=""):
    """
    Load data found in path to database.
    """

    data = Data.objects.get_all(uid=uid).first()
    # Work with existing data.
    if data:
        if update_toc:
            data.make_toc()
            print(f"*** Data id : {uid} table of contents updated.")
        return
    project = Project.objects.get_all(uid=pid).first()
    if not project:
        logger.error(f"Project id: {pid} does not exist.")
        return
    if not path or not os.path.exists(path):
        logger.error(f"--path ({path}) does not exist.")
        return
    # Slightly different course of action on file and directories.
    isdir = os.path.isdir(path)

    # Generate alternate names based on input directory type.
    print(f"*** Project: {project.name} ({project.uid})")
    if isdir:
        print(f"*** Linking directory: {path}")
        altname = os.path.split(path)[0].split(os.sep)[-1]
    else:
        print(f"*** Linking file: {path}")
        altname = os.path.split(path)[-1]

    # Get the text from file
    text = open(text, "r").read() if os.path.exists(text) else ""

    # Select the name.
    name = name or altname

    # Create the data.
    data = auth.create_data(project=project, path=path, type=type, name=name, text=text)
    print(f"*** Created data: name={name}, id={data.uid}")

    return data


def run(job, options={}):
    """
    Runs a job
    """
    # Options that cause early termination.
    show_json = options.get('show_json')
    show_template = options.get('show_template')
    show_script = options.get('show_script')
    show_command = options.get('show_command')
    use_template = options.get('use_template')
    use_json = options.get('use_json')
    verbosity = options.get('verbosity', 0)

    # Defined in case we bail on errors before setting it.
    script = command = proc = None

    stdout_log = []
    stderr_log = []
    try:
        # Find the json and the template.
        json_data = hjson.loads(job.json_text)
        template = job.template

        # This is the work directory.
        work_dir = job.path

        # The bade URL of the site.
        url_base = f'{settings.PROTOCOL}://{settings.SITE_DOMAIN}{settings.HTTP_PORT}'

        # Populate extra context
        def extra_context(job):
            extras = dict(
                media_root=settings.MEDIA_ROOT,
                media_url=settings.MEDIA_URL,
                work_dir=work_dir, local_root=settings.LOCAL_ROOT,
                user_id=job.owner.id, user_email=job.owner.email,
                job_id=job.id, job_name=job.name,
                job_url=f'{url_base}{settings.MEDIA_URL}{job.get_url()}'.rstrip("/"),
                project_id=job.project.id, project_name=job.project.name,
                analyis_name=job.analysis.name,
                analysis_id=job.analysis.id,
                domain=settings.SITE_DOMAIN, protocol=settings.PROTOCOL,
            )
            return extras

        # Add the runtime context.
        json_data['runtime'] = extra_context(job)

        # Override template.
        if use_template:
            template = open(use_template).read()

        # Override json.
        if use_json:
            json_data = hjson.loads(open(use_json).read())

        # Print the json.
        if show_json:
            print(hjson.dumps(json_data, indent=4))
            return

        # Print the template.
        if show_template:
            print(template)
            return

        # Extract the execute commands from the spec.
        settings_dict = json_data.get("settings", {})

        # Specifies the command that gets executed.
        execute = settings_dict.get('execute', {})

        # The name of the file that contain the commands.
        script_name = execute.get("filename", "recipe.sh")

        # Make the log directory that stores sdout, stderr.
        LOG_DIR = 'runlog'
        log_dir = os.path.join(work_dir, f"{LOG_DIR}")
        if not os.path.isdir(log_dir):
            os.mkdir(log_dir)

        # Runtime information will be saved in the log files.
        json_fname = f"{log_dir}/input.json"
        stdout_fname = f"{log_dir}/stdout.txt"
        stderr_fname = f"{log_dir}/stderr.txt"

        # Build the command line
        command = execute.get("command", "bash recipe.sh")

        # The commands can be substituted as well.
        context = Context(json_data)
        command_template = Template(command)
        command = command_template.render(context)

        # This is the full command that will be executed.
        full_command = f'(cd {work_dir} && {command})'
        if show_command:
            print(full_command)
            return

        # Script template.
        context = Context(json_data)
        script_template = Template(template)
        script = script_template.render(context)

        # Show the script.
        if show_script:
            print(f'{script}')
            return

        # Logging should start after the early returns.
        logger.info(f'Job id={job.id} name={job.name}')

        # Make the output directory
        logger.info(f'Job id={job.id} work_dir: {work_dir}')
        if not os.path.isdir(work_dir):
            os.mkdir(work_dir)

        # Create the script in the output directory.
        with open(os.path.join(work_dir, script_name), 'wt') as fp:
            fp.write(script)

        # Create a file that stores the json data for reference.
        with open(json_fname, 'wt') as fp:
            fp.write(hjson.dumps(json_data, indent=4))

        # Initial create each of the stdout, stderr file placeholders.
        for path in [stdout_fname, stderr_fname]:
            with open(path, 'wt') as fp:
                pass

        # Show the command that is executed.
        logger.info(f'Job id={job.id} executing: {full_command}')

        # Job must be authorized to run.
        if job.security != Job.AUTHORIZED:
            raise Exception(f"Job security error: {job.get_security_display()}")

        # Switch the job state to RUNNING and save the script field.
        Job.objects.filter(pk=job.pk).update(state=Job.RUNNING,
                                             start_date=timezone.now(),
                                             script=script)
        # Run the command.
        proc = subprocess.run(command, cwd=work_dir, shell=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # Raise an error if returncode is anything but 0.
        proc.check_returncode()

        # If we made it this far the job has finished.
        logger.info(f"uid={job.uid}, name={job.name}")
        Job.objects.filter(pk=job.pk).update(state=Job.COMPLETED)

    except Exception as exc:
        # Handle all errors here.
        Job.objects.filter(pk=job.pk).update(state=Job.ERROR)
        stderr_log.append(f'{exc}')
        logger.error(f'job id={job.pk} error {exc}')

    # Collect the output.
    if proc:
        stdout_log.extend(force_text(proc.stdout).splitlines())
        stderr_log.extend(force_text(proc.stderr).splitlines())

    # Save the logs and end time
    Job.objects.filter(pk=job.pk).update(end_date=timezone.now(),
                                         stdout_log="\n".join(stdout_log),
                                         stderr_log="\n".join(stderr_log))

    # Reselect the job to get refresh fields.
    job = Job.objects.filter(pk=job.pk).first()

    # Create a log script in the output directory as well.
    with open(stdout_fname, 'wt') as fp:
        fp.write(job.stdout_log)

    # Create a log script in the output directory as well.
    with open(stderr_fname, 'wt') as fp:
        fp.write(job.stderr_log)

    # Log job status.
    logger.info(f'Job id={job.id} finished, status={job.get_state_display()}')

    # Use -v 2 to see the output of the command.
    if verbosity > 1:
        print("-" * 40)
        print(job.stdout_log)
        print("-" * 40)
        print(job.stderr_log)

    if job.owner.profile.notify:

        context = dict(subject=job.project.name, job=job)

        # Send notification emails
        notify(template_name="emailer/job_finished.html", email_list=[job.owner.email], send=True,
               extra_context=context)


def list_obj(mtype="project"):

    mtype_map = dict(project=Project, recipe=Analysis, data=Data, job=Job)

    objs = mtype_map.get(mtype).objects.get_all().order_by("id")[:100]
    for instance in objs:
        print(f'{instance.id}\t{instance.uid}\t{instance.project.uid}\t{instance.name}')

    return


class Command(BaseCommand):
    help = 'Dump and load items using api.'

    def manage_job(self, **options):
        jobid = options.get('id')
        jobuid = options.get('uid')
        next = options.get('next')
        queued = options.get('list')

        # This code is also run insider tasks.
        if next:
            job = Job.objects.filter(state=Job.QUEUED).order_by('id').first()
            if not job:
                logger.info(f'there are no queued jobs')
            else:
                run(job, options=options)
            return

        if jobid or jobuid:

            job = Job.objects.filter(uid=jobuid) or Job.objects.filter(id=jobid)
            if not job:
                logger.info(f'job for id={jobid}/uid={jobuid} missing')
            else:
                run(job.first(), options=options)
            return

        if queued:
            jobs = Job.objects.get_all().order_by('id')[:100]
            for job in jobs:
                print(f'{job.id}\t{job.get_state_display()}\t{job.name}')
            return
        return

    def manage_load(self, **options):
        subcommand = sys.argv[2] if len(sys.argv) > 2 else None
        load = subcommand == "load"
        load_recipes = options.get("recipes")
        root_url = options.get("url")
        api_key = options.get("key")
        root_dir = options.get("dir") or os.getcwd()
        rid = options.get("rid")
        pid = options.get("pid")

        data = options.get("data")
        did = options.get("did")
        name = options.get("name")
        type = options.get("type")
        update_toc = options.get("update_toc")

        create_job = options.get("jobs")
        json_file = options.get("json")
        data_root = options.get("data_root")
        add_data = options.get("add_data")
        url_from_json = options.get("url_from_json")

        if ((root_url or url_from_json) and load) and not api_key:
            sys.argv.append("--help")
            self.stdout.write(self.style.NOTICE("[error] --key is required when loading data to remote site."))
            self.run_from_argv(sys.argv)
            sys.exit()

        if len(sys.argv) == 3:
            sys.argv.append("--help")
            self.run_from_argv(sys.argv)
            sys.exit()

        if data or did:
            if not (pid or did):
                self.stdout.write(self.style.NOTICE("[error] --pid or --did need to be set."))
                self.run_from_argv(sys.argv + ["--help"])
                sys.exit()
            data = data_loader(pid=pid, path=root_dir, uid=did, update_toc=update_toc, name=name, type=type)
            msg = f"{data.name} loaded into database."
            self.stdout.write(msg=self.style.SUCCESS(msg))
            return

        print(f"Loading json files from --dir {root_dir}")
        msg = f"{'loaded into url' if url_from_json or root_url else 'loaded into database'}."
        if load_recipes or rid:
            recipes = recipe_loader(root_dir=root_dir, root_url=root_url, api_key=api_key, json_fname=json_file,
                                    rid=rid, pid=pid, jobs=create_job, url_from_json=url_from_json)
            msg = f" {recipes} recipes {msg}"
            self.stdout.write(msg=self.style.SUCCESS(msg))
            return

        projects = project_loader(pid=pid, root_dir=root_dir, root_url=root_url, api_key=api_key, data=add_data,
                                  data_root=data_root, url_from_json=url_from_json, json_file=json_file)
        msg = f"{projects} projects {msg}."
        self.stdout.write(msg=self.style.SUCCESS(msg))
        return

    def manage_dump(self, **options):

        load_recipes = options.get("recipes")
        root_url = options.get("url")
        api_key = options.get("key")
        root_dir = options.get("dir") or os.getcwd()
        rid = options.get("rid")
        pid = options.get("pid")

        if (not (pid or rid)) or len(sys.argv) == 3:
            sys.argv.append("--help")
            self.stdout.write(self.style.NOTICE("--pid or --rid is required."))
            self.run_from_argv(sys.argv)
            sys.exit()

        print(f"Dumping from {root_url if root_url else 'database'}.")
        if load_recipes or rid:
            recipes = recipe_dumper(root_dir=root_dir, root_url=root_url, api_key=api_key, rid=rid, pid=pid)
            msg = f"{len(recipes)} recipes "
        else:
            project_dumper(pid=pid, root_dir=root_dir, root_url=root_url, api_key=api_key)
            msg = f"project id :{pid} "

        msg = msg + f"dumped into {root_dir}."
        self.stdout.write(msg=self.style.SUCCESS(msg))

        return

    def add_load_commands(self, parser):

        parser.add_argument('-u', "--url_from_json", action="store_true",
                            help="""Extract url from conf file instead of --url.""")
        parser.add_argument('-r', "--recipes", action="store_true",
                            help="""Load recipes of --pid""")
        parser.add_argument('-d', "--data", action="store_true",
                            help="""Load data of --pid to local database.""")
        self.add_api_commands(parser=parser)

        parser.add_argument("--add_data", action='store_true', help="Add data found in --json to --pid.")
        parser.add_argument("--jobs", action='store_true', help="Also creates a queued job for the recipe")
        parser.add_argument('--rid', type=str, default="", help="Recipe uid to load.")
        parser.add_argument("--pid", type=str, default="", help="Project uid to load from or dump to.")
        parser.add_argument("--did", type=str, help="Data uid to load or update.")

        parser.add_argument('--dir', default='', help="Base directory to store/load  from.")
        parser.add_argument("--path", type=str, help="Path to data.")
        parser.add_argument('--text', default='', help="A file containing the description of the data")
        parser.add_argument('--name', default='', help="Sets the name of the data")
        parser.add_argument('--type', default='data', help="Sets the type of the data")
        parser.add_argument("--update_toc", action="store_true", help="Update table of contents for data --uid.")

        parser.add_argument("--data_root", default="",
                            help="Root directory to data found in conf file when loading project.")
        parser.add_argument('--json', default='', help="""JSON file path relative to --dir to get conf from
                                                          ONLY when --load flag is set.""")
        return

    def add_dump_commands(self, parser):
        parser.add_argument('-r', "--recipes", action="store_true",
                            help="""Load recipes of --pid""")
        self.add_api_commands(parser=parser)

        parser.add_argument('--rid', type=str, default="", help="Recipe uid to dump.")
        parser.add_argument("--pid", type=str, default="", help="Project uid to load dump.")
        parser.add_argument('--dir', default='', help="Directory to store from.")
        return

    def add_api_commands(self, parser):
        """Add default api commands to parser"""
        parser.add_argument('--url', default="", help="Site url.")
        parser.add_argument('--key', default='', help="API key. Required to access private projects.")

        return

    def add_job_commands(self, parser):

        parser.add_argument('--next', action='store_true', default=False, help="Runs the oldest queued job")
        parser.add_argument('--id', type=int, default=0, help="Runs job specified by id.")
        parser.add_argument('--jid', type=str, default='', help="Runs job specified by uid.")
        parser.add_argument('--show_script', action='store_true', help="Shows the script.")
        parser.add_argument('--show_json', action='store_true', help="Shows the JSON for the job.")
        parser.add_argument('--show_template', action='store_true', help="Shows the template for the job.")
        parser.add_argument('--show_command', action='store_true', help="Shows the command executed for the job.")
        parser.add_argument('--use_json', help="Override the JSON with this file.")
        parser.add_argument('--use_template', help="Override the TEMPLATE with this file.")
        parser.add_argument('--list', action='store_true', help="Show a job list")

    def add_arguments(self, parser):

        subparsers = parser.add_subparsers()

        load_parser = subparsers.add_parser("load", help="""
                                                    Project, Data and Recipe load manager.
                                                    Project:  Create or update project in remote host or database.
                                                    Data:     Create or update data to project --pid in database. 
                                                    Recipe:   Create or update recipe in remote host or database. 
                                                    ."""
                                            )
        self.add_load_commands(parser=load_parser)

        dump_parser = subparsers.add_parser("dump", help="""
                                                    Project and Recipe Job dumper manager.
                                                    Project  : Dump project from remote host or database
                                                    Recipe:  : Dump Recipe from remote host or database.
                                                    """)
        self.add_dump_commands(parser=dump_parser)

        job_parser = subparsers.add_parser("job", help="Job manager.")
        self.add_job_commands(parser=job_parser)

    def handle(self, *args, **options):

        subcommand = sys.argv[2] if len(sys.argv) > 2 else None

        listing = options.get("list")

        if len(sys.argv) == 2:
            self.stdout.write(self.style.NOTICE("Pick a sub-command"))
            self.run_from_argv(sys.argv + ["--help"])
            sys.exit()

        if listing:
            list_obj(mtype=subcommand)
            return

        if subcommand == "load":
            self.manage_load(**options)
            return

        if subcommand == "dump":
            self.manage_dump(**options)
            return

        if subcommand == "job":
            self.manage_job(**options)
            return
