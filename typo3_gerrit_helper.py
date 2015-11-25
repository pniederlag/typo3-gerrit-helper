#! /usr/bin/env python
'''
Created on 22.03.2012

@author: pn
'''
import argparse
import shlex
import json
import shutil
import glob
import os
import sys, traceback
from subprocess import check_call, check_output, CalledProcessError, STDOUT
import tempfile
import re
from ConfigParser import SafeConfigParser
from string import Template

class Typo3GerritHelper():
    '''
    some helper to create git repos and gerrit projects from forge projects
    pretty much tied to the infrastructure of the TYPO3 project

    @requirements:

        * gerrit account with group membership 'admin' and admin privileges (later refered to as "robotuser")
        * ssh tunel 3309:127.0.0.1:3306 onto srv137.typo3.org (mysql on forge)
        * copy .secret.example.cfg to .secret.example.cfg and adjust settings
        ** set db credentials for forge
        ** robotuser on gerrit/review with admin privileges'
        * [ssh config  (user) for hosts: review.typo3.org, srv137.typo3.org (obsoleted?)]

    @todo:

        * make ssh and hosts configurable
        * manage the ssh tunnel
        * get secrets from forge server
        * add svn2git conversion
        * map the forge_identifier into the git path
        * review API/Language Implementation
        * ask whether to enable review workflow

    '''

    def __init__(self,args):
        '''
        Constructor
        '''
        self.git_remote_url = 'review.typo3.org'
        #self.git_remote_url = 'ssh:\/\/jugglepro@review.local:29418'

        review_host = 'review.typo3.org'
        server_host = 'git.typo3.org'

        self.git_repo_path = '/var/git/repositories'

            #self.forge_db_id will be set in get_check_forge_id
        self.forge_db_id = False
            #self.old_svn_path will be set somewhere below
        self.old_svn_path = False
        self.forge_repo_path = '/var/git/repositories'

        parser = SafeConfigParser()
            # .secret
        parser.read('.secret.cfg')
        self.forge_db = parser.get('forge', 'db')
        self.forge_user = parser.get('forge', 'user')
        self.forge_pw = parser.get('forge', 'pw')
        os.environ["MYSQL_PWD"] = self.forge_pw
        self.robot_user = parser.get('gerrit', 'robot_user')

        self.ssh_cmd =        'ssh ' + server_host
        self.gerrit_ssh_cmd = 'ssh ' + self.robot_user + '@' + review_host + ' -p 29418'

	self.create_project_command = 'create-project --require-change-id --submit-type CHERRY_PICK --empty-commit'

            # config
        parser.read('config.cfg')
        self.interactive = parser.getboolean('config', 'interactive')
        if args.interactive_false:
            self.interactive = False
        self.debug = parser.get('config', 'debug')

    def run(self,forge_identifier,git_path):
        self.forge_identifier = forge_identifier

            # strip '.git' from git_path in case it was set
        regex = re.compile('(.*)(\.git)',re.UNICODE)
        matches=regex.search(git_path)
        if matches:
            self.git_path = matches.group(1)
        else:
            self.git_path = git_path
        print self.git_path

        temp_dir = None
        for test_temp_dir in glob.glob('/tmp/t3git-*'):
            try:
                test_config_file = open(test_temp_dir + '/.git/config')
                regex = re.compile(git_path,re.UNICODE)
                matches=regex.search(test_config_file.read())
                if matches:
                    temp_dir = test_temp_dir
                    print 'temp_dir ' + temp_dir + ' will be reused'
            except:
                pass
        if not temp_dir:
            self.tmp_dir = tempfile.mkdtemp(prefix='t3git-')
        else:
            self.tmp_dir = temp_dir

        self.get_check_forge_identifier()
        self.get_repository_in_forge()

            # start real work
        self.create_groups()
        self.create_project()
        self.update_project_config()
        #self.migrate_svn_to_git()
        #self.cleanup_svn_repo()
        #self.update_repository_in_forge()

            # cleanup
        self.cleanup_tmpdir()

    def cleanup_tmpdir(self):
        default = "YES"
        if self.interactive == False:
            shutil.rmtree(self.tmp_dir)
        else:
            user_input = raw_input('# Do you want to cleanup the temp directory "{0}"? [{1}|no]: '.format(self.tmp_dir,default))
            if not user_input or user_input == default:
                shutil.rmtree(self.tmp_dir)
            else:
                print '# need to cleanup "{0}" yourself'.format(self.tmp_dir)

    def create_groups(self):
        '''
        adding a proper group for the project into gerrit including adding the proper forge_project_id
        '''
        group_leaders = self.git_path + '-Leaders'
        self.create_group(group_leaders, 'Administrators')

        group_members = self.git_path + '-Members'
        self.create_group(group_members, group_leaders)

    def create_group(self, group_name, owner):
        # figure out, if there is already a group called like site.git_path
        output = self.gerrit_ssh('ls-groups')
        regex = re.compile("^" + group_name + "$",re.MULTILINE|re.UNICODE)
        projects = regex.findall(output)
        count = len(projects)

        if count == 0:
            print '# will create Group "' + group_name + '" in gerrit'
            self.gerrit_ssh('create-group --owner Administrators ' + group_name)
        elif count == 1:
            print '# Group "' + group_name + '" is already known to gerrit'
        else:
            raise Exception('# querying gerrit for the group "' + group_name + '" failed for an unknown reason')
        self.gerrit_ssh('gsql -c \\\"update account_group_names set forge_project_id=\\\'' +  self.forge_identifier + '\\\' where name=\\\'' + group_name + '\\\' limit 1\\\"')


    def get_check_forge_identifier(self):
        '''
        try to find the id of the project in forge database by looking up the provided forge_identifier
        we always do this and bail out in case this is not succesfull
        '''
        try:
            query = 'select id from projects where identifier=\'' + self.forge_identifier + '\''
            cmd =  'mysql -u ' + self.forge_user + ' -h 127.0.0.1 -P 3309 ' + ' -e "' + query + '" ' + self.forge_db
            output = self.execute(cmd)
            lines = output.splitlines()
            self.forge_db_id = int(lines[1])
        except Exception as ex:
            msg = 'ERROR: project "' + self.forge_identifier + '" not found. pls check it is valid\n'
            msg += '#\n'
            msg += '# Did you setup an ssh tunnel for mysql on port 3309 to srv108?\n'
            msg += '# ssh -N -L 3309:127.0.0.1:3306 srv108\n'
            msg += '#'
            raise Exception(msg)

    def get_repository_in_forge(self):
        old_svn_path = False
        rep_id = False
        query = 'select id, url from repositories where project_id=' + str(self.forge_db_id) + ''
        try:
            output = self.execute(
                        'mysql' +
                        ' -u ' + self.forge_user +
                        ' -h 127.0.0.1' +
                        ' -P 3309' +
                        ' -e "' + query + '"' +
                        ' ' + self.forge_db
                        )
            lines = output.splitlines()
            [rep_id, old_svn_path] = lines[1].split('\t')
        except Exception as ex:
            #db_id = False
            print '# no repository for "' + self.forge_identifier + '" found. creation of new repo in forge is not supported yet.'
            return
        # check wether we have any rep at all
        if not old_svn_path or not rep_id:
            print '# no attached repo found on forge'
            return
        # check wether we still have an svn rep
        if not old_svn_path.startswith('https://svn'):
            print '# attached repo "' +  old_svn_path + '" seems not to be svn, maybe already git?'
        elif old_svn_path.endswith('/'):
            self.old_svn_path = old_svn_path
        else:
            self.old_svn_path = old_svn_path + '/'
        self.forge_rep_id = rep_id

    def update_repository_in_forge(self):
        if not self.forge_rep_id:
            print '#'
            print '# can\'t update the repository in forge as it is unknown. probably get_repository in forge has not been run?'
            return

        query = 'update repositories set url=\'' + self.forge_repo_path + '/' + self.git_path + '.git\' , root_url=\'' + self.forge_repo_path + '/' + self.git_path + '.git\', type=\'Git\' where id=' +  self.forge_rep_id + ''
        output = self.confirm_execute(
                    'mysql' +
                    ' -u ' + self.forge_user +
                    ' -h 127.0.0.1' +
                    ' -P 3309' +
                    ' -e "' + query + '"' +
                    ' ' + self.forge_db
                    )
        if output:
            lines = output.splitlines()
            # what to do with the result output?

    def migrate_svn_to_git(self):
        # ssh review.typo3.org gerrit gsql --format=JSON -c \"select accounts.full_name, accounts.preferred_email from account_external_ids join accounts on accounts.account_id=account_external_ids.account_id where external_id=\'username:fab1en\' limit 1\"
        push_url = self.robot_user + '@' + self.git_remote_url + ':' + self.git_path + '.git'
        try:
            push_branches = []
            if not self.old_svn_path:
                print '#'
                print '# can\'t cleanup the svn as old_svn_path is not known.'
                return
            self.execute('git init', cwd=self.tmp_dir)
            self.execute('git svn init -s --prefix=svn/ ' + self.old_svn_path, cwd=self.tmp_dir)
            self.execute('git svn fetch', cwd=self.tmp_dir)
            all_refs = self.execute('git show-ref', cwd=self.tmp_dir)
            for ref in all_refs.splitlines():
                [sha1, symbolic_name] = ref.split(' ')
                regex = re.compile('(?P<svn>refs/remotes/svn/)(?P<name>[^\/]+)$',re.UNICODE)
                matches=regex.search(symbolic_name)
                if matches:
                    branch = matches.group(2).replace('trunk', 'master');
                    push_branches.append(branch)
                    self.execute('git update-ref refs/heads/' + branch + ' ' + sha1, cwd=self.tmp_dir)
                regex = re.compile('(?P<svn>refs/remotes/svn/)(?P<tags>tags/)(?P<name>[a-zA-Z0-9-_.]+)',re.UNICODE)
                matches=regex.search(symbolic_name)
                if matches:
                    tag = matches.group(3)
                    self.execute('git tag -f ' + tag + ' ' + sha1, cwd=self.tmp_dir)

            for push_branch in push_branches:
                self.confirm_execute('git push ' + push_url + ' refs/heads/'+ push_branch, cwd=self.tmp_dir)
            self.confirm_execute('git push ' + push_url + ' --tags', cwd=self.tmp_dir)
        except Exception:
            self.old_svn_path = False
            raise
        # for now we set it to False to preven updating the rep in svn and forge
        # we should do this unless we are sure migration was successfull
        self.old_svn_path = False


    def cleanup_svn_repo(self):
        if not self.old_svn_path:
            print '#'
            print '# can\'t cleanup svn as no old_svn_path could be found. This happens for example if the rep in forge already points to git'
            return
        new_git_url = 'http://git.typo3.org/' + self.git_path + '.git'
        svn_info = self.execute('svn info ' + self.old_svn_path)
        regex = re.compile('(Revision: )([0-9]+)',re.UNICODE)
        matches=regex.search(svn_info)
        if matches:
            rev_no = matches.group(2)
        else:
            raise Exception('can\'t find the latest svn revision for ' + self.old_svn_path)

        commit_msg = 'Moved to git ' + new_git_url + ''

        directories = self.execute('svn ls ' + self.old_svn_path)
        if directories:
            cmd = 'svn rm -m "' + commit_msg + '"'
            for obsolete_dir in directories.splitlines():
                cmd = cmd + ' ' + self.old_svn_path + obsolete_dir
            self.confirm_execute(cmd)

        readme_template = Template(open('README.removed.template', "r").read())
        readme_file = tempfile.NamedTemporaryFile(delete=True)
        readme_content = readme_template.substitute(new_git_url=new_git_url, svn_rev_no=rev_no, svn_old_path=self.old_svn_path)
        readme_file.writelines([readme_content])
        readme_file.seek(0) # rewind
        cmd = 'svn import -m "' + commit_msg + '" ' + readme_file.name + ' ' + self.old_svn_path + 'README'
        self.confirm_execute(cmd)
        readme_file.close()


    def uuid_for_group(self, group_name):
        # find out the uuid of group_name
        output = self.gerrit_ssh('ls-groups -v')
        # output is a tab-separated list of "<group-name>	<uuid>	<whatever>" lines
        # search in that text for the line matching group_name in the first column and return the uuid in the second column
        for line in output.splitlines():
            linedata = line.split("\t")
            if linedata[0] == group_name:
                return linedata[1]

    def create_project(self):
        output = self.gerrit_ssh('ls-projects')
        lines = output.splitlines()
        try:
            found = lines.index(self.git_path)
            print '# project has already been created'
        except (ValueError, LookupError):
            print '# will create project now'
            self.gerrit_ssh(self.create_project_command + ' ' + self.git_path)
        #  touch the git-daemon-export-ok file to allow git browsing
        cmd = self.ssh_cmd + ' -t sudo touch "' + self.git_repo_path + '/' + self.git_path + '.git/git-daemon-export-ok"'
        self.execute(cmd, call_only=True)

    def gerrit_ssh(self, cmd):
        return self.execute(self.gerrit_ssh_cmd + ' gerrit ' + cmd)


    def execute(self, cmd, cwd=None, call_only=False):
        output = None
        args = shlex.split(cmd)
        print '$ ' + cmd
        if call_only == True:
            check_call(args=args, cwd=cwd)
        else:
            try:
                output = check_output(args=args, cwd=cwd, stderr=STDOUT)
            except CalledProcessError as cperr:
                #print cperr.output
                raise Exception('"{0}" failed with: "{1}"'.format(cmd,cperr.output))
        if output and self.debug == 'True':
            for line in output.splitlines():
                print line
            #print output
        return output

    def confirm_execute(self,cmd, cwd=None, call_only=False):
        output = None
        if self.interactive == False:
            output = self.execute(cmd, cwd, call_only)
        else:
            print '############################################################'
            print '# Need your confirmation for the next command to perform:'
            print cmd
            output = None
            default = "YES"
            user_input = raw_input("# Do you want to execute above command? [%s|no]: " % default)
            if not user_input or user_input == default:
                print '# will execute'
                print '############################################################'
                output = self.execute(cmd, cwd, call_only)
            else:
                print '# INFO: skipping execution as requested!'
                print '############################################################'

        return output

    def update_project_config(self):

        group_leaders_name = self.git_path + '-Leaders'
        group_members_name = self.git_path + '-Members'
        group_leaders_uuid = self.uuid_for_group(group_leaders_name)
        group_members_uuid = self.uuid_for_group(group_members_name)

        # FIXME, unfortunatly git remotes with ssh/config use ':' as first separator, while andthing else needs '/'
        #origin = self.git_remote_url + '/' + self.git_path + '.git'
        origin = self.robot_user + '@' + self.git_remote_url + ':' + self.git_path + '.git'

        self.execute('git init', cwd=self.tmp_dir)
        # check for existance of remote "origin", create remote otherwise
        try:
            self.execute('git remote show origin ' + origin, cwd=self.tmp_dir)
        except:
            self.execute('git remote add origin ' + origin, cwd=self.tmp_dir)
        self.execute('git fetch origin refs/meta/config:refs/remotes/origin/meta/config', cwd=self.tmp_dir)
        self.execute('git checkout meta/config', cwd=self.tmp_dir)
        #self.execute('git reset --hard origin/meta/config', cwd=self.tmp_dir)

	###############################
	# Leaders + Members
	###############################

        self.execute('git config --file ' + self.tmp_dir + '/project.config --replace-all "access.refs/heads/*.label-Code-Review" "-2..+2 group ' + group_leaders_name + '"', cwd=self.tmp_dir)
        self.execute('git config --file ' + self.tmp_dir + '/project.config --add "access.refs/heads/*.label-Code-Review" "-2..+2 group ' + group_members_name + '"', cwd=self.tmp_dir)

        self.execute('git config --file ' + self.tmp_dir + '/project.config --replace-all "access.refs/heads/*.label-Verified" "-1..+2 group ' + group_leaders_name + '"', cwd=self.tmp_dir)
        self.execute('git config --file ' + self.tmp_dir + '/project.config --add "access.refs/heads/*.label-Verified" "-1..+2 group ' + group_members_name + '"', cwd=self.tmp_dir)

        self.execute('git config --file ' + self.tmp_dir + '/project.config --replace-all "access.refs/heads/*.submit" "group ' + group_leaders_name + '"', cwd=self.tmp_dir)
        self.execute('git config --file ' + self.tmp_dir + '/project.config --add "access.refs/heads/*.submit" "group ' + group_members_name + '"', cwd=self.tmp_dir)

        self.execute('git config --file ' + self.tmp_dir + '/project.config --replace-all "access.refs/for/refs/heads/*.pushMerge" "group ' + group_leaders_name + '"', cwd=self.tmp_dir)
        self.execute('git config --file ' + self.tmp_dir + '/project.config --add "access.refs/for/refs/heads/*.pushMerge" "group ' + group_members_name + '"', cwd=self.tmp_dir)

	###############################
	# Leaders only
	###############################

	# annotated tags
        self.execute('git config --file ' + self.tmp_dir + '/project.config --replace-all "access.refs/tags/*.pushTag" "group ' + group_leaders_name + '"', cwd=self.tmp_dir)

	# lightweight tags
        self.execute('git config --file ' + self.tmp_dir + '/project.config --replace-all "access.refs/tags/*.create" "group ' + group_leaders_name + '"', cwd=self.tmp_dir)

	# owner privileges
        self.execute('git config --file ' + self.tmp_dir + '/project.config --replace-all "access.refs/*.owner" "group ' + group_leaders_name + '"', cwd=self.tmp_dir)

	# create branches
        self.execute('git config --file ' + self.tmp_dir + '/project.config --replace-all "access.refs/heads/*.create" "group ' + group_leaders_name + '"', cwd=self.tmp_dir)

	# forge committer identity
        self.execute('git config --file ' + self.tmp_dir + '/project.config --replace-all "access.refs/for/refs/heads/*.forgeCommitter" "group ' + group_leaders_name + '"', cwd=self.tmp_dir)

        group_lines=[
             '# UUID                                  \tGroup Name\n',
             '#\n',
             group_leaders_uuid + '\t' + group_leaders_name + '\n',
             group_members_uuid + '\t' + group_members_name + '\n',
             ]
        groups_file = open(self.tmp_dir + '/groups', "w")
        groups_file.writelines(group_lines)
        groups_file.close()
        self.execute('git add project.config groups', cwd=self.tmp_dir)
        diff = self.execute('git diff --cached origin/meta/config', cwd=self.tmp_dir)
        if diff == '':
            print '# permissions need NO update'
        else:
            print '# updating permissions to default'
            self.execute('git commit -m "Default Permissions"', cwd=self.tmp_dir)
            self.confirm_execute('git push origin meta/config:refs/meta/config', cwd=self.tmp_dir)



parser = argparse.ArgumentParser(
                                 description='''TYPO3 helper to create gerrit projects based on forge projects

    Example:  %(prog)s extension-foo_bar TYPO3CMS/Extensions/foo
''',
                            formatter_class=argparse.RawDescriptionHelpFormatter
                            )

parser.add_argument('forge_identifier', nargs='?',
                   help='identifier string of forge project')
parser.add_argument('git_path', nargs='?',
                   help='path inside git structure')
parser.add_argument('-f','--file', type=open,
                   help='file name with lines of fields forge_id and git_path, fields must be separated by <tab>')
parser.add_argument('-y','--interactive-false',
                   action='store_true',
                   help='unset interactive mode, overrides value from config.cfg')

args = parser.parse_args()

gerrit_helper = Typo3GerritHelper(args)

if args.file:
    try:
        #project_file = open(args.file)
        failed_projects = []
        for line in args.file.read().splitlines():
            [forge_identifier,git_path] = line.split('\t')
            #print forge_id + ' ' + git_path
            try:
                print '##############################################'
                print '# start processing "{0}" "{1}"'.format(forge_identifier, git_path)
                print '##############################################'
                gerrit_helper.run(forge_identifier, git_path)
            except Exception as ex:
                print ''
                print '############################################################################'
                print '# ERROR: project "{0}" "{1}" could not be processed'.format(forge_identifier, git_path)
                print '#'
                print '# {0}'.format(ex)
                ex_type, ex, tb = sys.exc_info()
                traceback.print_tb(tb)
                print '############################################################################'
                print ''
                failed_projects.append(forge_identifier)
        if failed_projects:
            print ''
            print '!!!!!! projects that failed: !!!!!!!'
            print failed_projects
    except Exception as ex:
        print ''
        print '############################################################################'
        print '# ERROR: could not read/iterate --file={0}'.format(args.file)
        print '#        fields should be terminated by <tab>'
        print '#'
        print '# {0}'.format(ex)
        print '############################################################################'
        print ''
        parser.print_help()
elif (args.forge_identifier != None and args.git_path != None):
    try:
        forge_identifier = args.forge_identifier
        git_path = args.git_path
        print '##############################################'
        print '# start processing "{0}" "{1}"'.format(forge_identifier, git_path)
        print '##############################################'
        gerrit_helper.run(forge_identifier, git_path)
    except Exception as ex:
        print ''
        print '############################################################################'
        print '# ERROR: project "{0}" "{1}" could not be processed'.format(forge_identifier, git_path)
        print '#'
        print '# {0}'.format(ex)
        ex_type, ex, tb = sys.exc_info()
        traceback.print_tb(tb)
        print '############################################################################'
        print ''
else:
    print '############################################################################'
    print '# ERROR: you must either provide forge_identifier and git_id or --file=SomeFile'
    print '#'
    print '############################################################################'
    print ''
    parser.print_help()
