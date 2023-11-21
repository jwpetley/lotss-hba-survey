#!/usr/bin/python

# clone of monitor.py to do monitoring of full field reprocessing

from __future__ import print_function
from time import sleep
import datetime
from surveys_db import SurveysDB, tag_field, get_cluster

## need to update this
#from run_full_field_reprocessing_pipeline import stage_field
#from reprocessing_utils import prepare_field  ## ddf-pipeline utils
import os
import threading
import glob
import requests
import stager_access
from rclone import RClone   ## DO NOT pip3 install --user python-rclone -- use https://raw.githubusercontent.com/mhardcastle/ddf-pipeline/master/utils/rclone.py


#################################
## CLUSTER SPECIFICS - use environment variables

'''
export DDF_PIPELINE_CLUSTER=cosma
export LINC_DATA_DIR=/cosma5/data/durham/dc-mora2/surveys/
export MACAROON_DIR=/cosma/home/durham/dc-mora2/macaroons/

export DDF_PIPELINE_CLUSTER=spider
export LINC_DATA_DIR=/project/lofarvlbi/Share/surveys
export MACAROON_DIR=/home/lofarvlbi-lmorabito/macaroons/

export DDF_PIPELINE_CLUSTER=azimuth
export LINC_DATA_DIR=/home/azimuth/surveys/
export MACAROON_DIR=/home/azimuth/macaroons/
export LOFAR_SINGULARITY=/home/azimuth/software/singularity/lofar_sksp_v4.4.0_cascadelake_cascadelake_ddf_mkl_cuda.sif
'''

user = os.getenv('USER')
if len(user) > 20:
    user = user[0:20]
cluster = os.getenv('DDF_PIPELINE_CLUSTER')
basedir = os.getenv('LINC_DATA_DIR')
procdir = os.path.join(basedir,'processing')

download_thread=None
download_name=None
stage_thread=None
stage_name=None
unpack_thread=None
unpack_name=None
verify_thread=None
verify_name=None
totallimit=3 #Total number of fields being processed
staginglimit=1 #Number of LTA to stage at any time
maxstaged=1 #Number of LTA to stage at any time

## cluster specific queuing limits
if cluster == 'spider':
    maxqueue = 10
if cluster == 'cosma':
    maxqueue = 1

'''
updated in MySQL_utils.py:
update_status
get_lbcalibrator
set_lbcalibrator
'''

def update_status(name,status,stage_id=None,time=None,workdir=None,av=None,survey=None):
    # adapted from surveys_db
    # utility function to just update the status of a field
    # name can be None (work it out from cwd), or string (field name)
    id=name 
    with SurveysDB(survey=survey) as sdb:
        idd=sdb.db_get('lb_fields',id)
        if idd is None:
          raise RuntimeError('Unable to find database entry for field "%s".' % id)
        idd['status']=status
        tag_field(sdb,idd,workdir=workdir)
        if time is not None and idd[time] is None:
            idd[time]=datetime.datetime.now()
        if stage_id is not None:
            idd['staging_id']=stage_id
        sdb.db_set('lb_fields',idd)
    sdb.close()        

##############################
## staging

def stage_cal( id, survey=None ):
    with SurveysDB(survey=survey) as sdb:
        idd = sdb.db_get('lb_fields',id)
    srmfilename = idd['srmfile']
    response = requests.get(srmfilename)
    data = response.text
    uris = data.rstrip('\n').split('\n')
    stage_id = stager_access.stage(uris)
    update_status(id, 'Staging', stage_id=stage_id )

##############################
## downloading

def do_download( id ):
    update_status(id,'Downloading')
    ## get the staging id from the surveys database
    with SurveysDB(readonly=True) as sdb:
        idd=sdb.db_get('lb_fields',id)
        stage_id = idd['staging_id']
    sdb.close()
    ## get the surls from the stager API
    surls = stager_access.get_surls_online(stage_id)
    project = surls[0].split('/')[-3]
    obsid = surls[0].split('/')[-2]
    obsid_path = os.path.join(project,obsid)
    if len(surls) > 0:
        caldir = os.path.join(str(os.getenv('LINC_DATA_DIR')),str(id))
        os.makedirs(caldir,exist_ok=True)
        if 'juelich' in surls[0]:
            for surl in surls:
                dest = os.path.join(caldir,os.path.basename(surl))
                os.system('gfal-copy {:s} {:s} > {:s}_gfal.log 2>&1'.format(surl.replace('srm://lofar-srm.fz-juelich.de:8443','gsiftp://lofar-gridftp.fz-juelich.de:2811'),dest,id))
            os.system('rm {:s}_gfal.log'.format(id))
        if 'psnc' in surls[0]:
            for surl in surls:
                dest = os.path.join(caldir,os.path.basename(surl))
                os.system('gfal-copy {:s} {:s} > {:s}_gfal.log 2>&1'.format(surl.replace('srm://lta-head.lofar.psnc.pl:8443','gsiftp://gridftp.lofar.psnc.pl:2811'),dest,id))
            os.system('rm {:s}_gfal.log'.format(id))
        if 'sara' in surls[0]:
            ## can use a macaroon
            files = [ os.path.basename(val) for val in surls ]
            macaroon_dir = os.getenv('MACAROON_DIR')        
            lta_macaroon = glob.glob(os.path.join(macaroon_dir,'*LTA.conf'))[0]
            rc = RClone( lta_macaroon, debug=True )
            rc.get_remote()
            #d = rc.multicopy(rc.remote+obsid_path,files,caldir)
            for f in files:
                d = rc.execute(['-P','copy',rc.remote + os.path.join(obsid_path,f)]+[caldir]) 
            if d['err'] or d['code']!=0:
                update_status(field,'rclone failed')
                print('Rclone failed for field {:s}'.format(field))
        ## check that everything was downloaded
        tarfiles = glob.glob(os.path.join(caldir,'*tar'))
        if len(tarfiles) == len(surls):
            print('Download successful for {:s}'.format(id) )
            update_status(id,'Downloaded',stage_id=0)
    else:
        print('SURLs do not appear to be online for {:s} (staging id {:s})'.format(id,str(stage_id)))
        update_status(id,'Download failed')

##############################
## unpacking

def dysco_compress(caldir,msfile):
    msfile = os.path.join(caldir,msfile)
    success=True
    with open(os.path.join(caldir,'dysco_compress.parset'),'w') as f:
        f.write('msin={:s}\n'.format(msfile))
        f.write('msin.datacolumn=DATA\n')
        f.write('msout={:s}.tmp\n'.format(msfile))
        f.write('msout.datacolumn=DATA\n')
        f.write('msout.storagemanager=dysco\n')
        f.write('steps=[count]')
    sing_img = os.getenv('LOFAR_SINGULARITY')
    os.system('singularity exec -B {:s} {:s} DP3 {:s}'.format(os.getcwd(),sing_img,os.path.join(caldir,'dysco_compress.parset')))
    if os.path.exists('{:s}.tmp'.format(msfile)):
        os.system('rm -r {:s}'.format(msfile))
        os.system('mv {:s}.tmp {:s}'.format(msfile,msfile))
    else:
        print('something went wrong with dysco compression for {:s}'.format(msfile))
        success=False
    return(success)

    
def do_unpack(field):
    update_status(field,'Unpacking')
    success=True
    do_dysco=False
    caldir = os.path.join(str(os.getenv('LINC_DATA_DIR')),field)
    ## get the tarfiles
    tarfiles = glob.glob(os.path.join(caldir,'*tar'))
    ## check if needs dysco compression
    gb_filesize = os.path.getsize(tarfiles[0])/(1024*1024*1024)
    if gb_filesize > 40.:
        do_dysco = True
    for trf in tarfiles:
        os.system( 'tar -xvf {:s} >> {:s}_unpack.log 2>&1'.format(trf,field) )
        msname = '_'.join(os.path.basename(trf).split('_')[0:-1])
        os.system( 'mv {:s} {:s}'.format(msname,caldir))
        if do_dysco:
            dysco_success = dysco_compress(caldir,msname)
            ## ONLY FOR NOW
            if dysco_success:
                os.system('rm {:s}'.format(trf))
                
    ## check that everything unpacked
    msfiles = glob.glob('{:s}/L*MS'.format(caldir))
    if len(msfiles) == len(tarfiles):
        update_status(field,'Unpacked')
        os.system('rm {:s}/*.tar'.format(caldir))
        os.system('rm {:s}_unpack.log'.format(field))
    else:
        update_status(field,'Unpack failed')

##############################
## verifying

def check_field(field):
    procdir = os.path.join(str(os.getenv('LINC_DATA_DIR')),'processing')
    outdir = os.path.join(procdir,field)
    if os.path.isfile(os.path.join(outdir,'cal_solutions.h5')):
        os.system('tar cvzf {:s}.tgz {:s}/inspection {:s}/*.json {:s}/cal_solutions.h5'.format(field,outdir,outdir,outdir))
        os.system('rm -rf {:s}/tmp*'.format(outdir))
        success = True
    else:
        success = False
    return success

def do_verify(field):
    tarfile = glob.glob(field+'*tgz')[0]
    macaroon_dir = os.getenv('MACAROON_DIR')
    macaroon = glob.glob(os.path.join(macaroon_dir,'*lofarvlbi_upload.conf'))[0]
    rc = RClone( macaroon, debug=True )
    rc.get_remote()
    d = rc.execute_live(['-P', 'copy', tarfile]+[rc.remote + '/' + 'disk/surveys/'])
    if d['err'] or d['code']!=0:
        update_status(field,'rclone failed')
        print('Rclone failed for field {:s}'.format(field))
    else:
        print('Tidying uploaded directory for',field)
        update_status(field,'Complete')
        ## delete the directory
        os.system( 'rm -r {:s}'.format(os.path.join(procdir,field)))
        ## delete the initial data
        os.system( 'rm -r {:s}'.format(os.path.join(basedir,field)))
        ## delete the tarfile
        os.system( 'rm {:s}.tgz'.format(field))

''' Logic is as follows:

1. if there is a not started dataset, first operation is always to stage a dataset (NB a different operation if it's on rclone or on SDR -- can do SDR first). At most one staging thread. Set status to Staged on sucessful complete. From this point on we only look for datasets that are associated with the local cluster.

2. any Staged dataset can be downloaded. At most one download thread: set status to Downloaded on successful complete

3. any Downloaded dataset can be unpacked. Set status to Unpacked when done.

4. any Unpacked dataset can have processing run on it. Set status to Started on start. (status set to Verified on upload)

    Processing:
        - pre-facet-target (if needed)
            --> verify, tidy
        - ddf-pipeline light (if needed)
            --> verify, tidy
        - delay calibration
            --> verify, tidy
        - split directions
            --> verify, tidy
        - 1" imaging 
            --> verify, tidy

5. upload data products back
'''

while True:

    myPointings = ['P348+33',
                    'P250+33',
                    'P201+25',
                    'P011+29',
                    'P144+29',
                    'P180+62',
                    'P30Hetdex06',
                    'P152+62',
                    'P177+37',
                    'P137+47']

    with SurveysDB(readonly=True) as sdb:
        #### CHANGE QUERIES TO USE TARGET TABLE
        sdb.cur.execute('select * from lb_fields where clustername="'+cluster+'" and username="'+user+'" order by priority,id')
        result=sdb.cur.fetchall()
        sdb.cur.execute('select * from lb_fields where status="Not started" and priority>0 order by priority,id')
        result2=sdb.cur.fetchall()
        if len(result2)>0:
            nextfield=result2[0]['id']
            for res in result2:
                if res['id'] in myPointings:
                    nextfield = res['id']
                    break
        else:
            nextfield=None

    d={} ## len(fd)
    fd={}  ## dictionary of fields of a given status type
    for r in result:
        status=r['status']
        if status in d:
            d[status]=d[status]+1
            fd[status].append(r['id'])
        else:
            d[status]=1
            fd[status]=[r['id']]
    d['Not started']=len(result2)
    print('\n\n-----------------------------------------------\n\n')
    print('LB target reprocessing status on cluster %s' % (cluster))
    print(datetime.datetime.now())
    print()
    failed=0
    for k in sorted(d.keys()):
        print("%-20s : %i" % (k,d[k]))
        if 'ailed' in k:
            failed+=d[k]

    print()
    ksum=(len(glob.glob(basedir+'/*'))-4)-failed
    if ksum<0: ksum=0
    print(ksum,'live directories out of',totallimit)
    print('Next field to work on is',nextfield)

    if download_thread is not None:
        print('Download thread is running (%s)' % download_name)
    if unpack_thread is not None:
        print('Unpack thread is running (%s)' % unpack_name)
    if stage_thread is not None:
        print('Stage thread is running (%s)' % stage_name)
    if verify_thread is not None:
        print('Verify thread is running (%s)' % verify_name)

    if download_thread is not None and not download_thread.is_alive():
        print('Download thread seems to have terminated')
        download_thread=None

    if unpack_thread is not None and not unpack_thread.is_alive():
        print('Unpack thread seems to have terminated')
        unpack_thread=None

    if stage_thread is not None and not stage_thread.is_alive():
        print('Stage thread seems to have terminated')
        stage_thread=None

    if verify_thread is not None and not verify_thread.is_alive():
        print('Verify thread seems to have terminated')
        verify_thread=None

    ## need to start staging if: staging isn't happening -or- staging is happening but less than staging limit
    if 'Staging' in d.keys():
        nstage = d['Staging']
    else:
        nstage = 0
    if 'Staged' in d.keys():
        nstaged = d['Staged']
    else:
        nstaged = 0
    check_stage = (nstage < staginglimit) + (nstaged < maxstaged)
    print(check_stage)
    if check_stage == 1 or check_stage == 0:
        do_stage = False
    else:
        do_stage = True

    ## do we want to check that calibrator solutions exist first?

    if do_stage and nextfield is not None:
        stage_name=nextfield
        print('We need to stage a new field (%s)' % stage_name)
        stage_cal(stage_name)

    if 'Staging' in d.keys():
        ## get the staging ids and then check if they are complete
        ## loop over ids and call database to get staging id
        for field in fd['Staging']:
            ## get the stage id
            r = [ item for item in result if item['id'] == field ][0]
            s = r['staging_id']
            stage_status = stager_access.get_status(s)
            #    “new”, “scheduled”, “in progress”, “aborted”, “failed”, “partial success”, “success”, “on hold” 
            if stage_status == 'success' or stage_status == 'completed':
                print('Staging for {:s} is complete, updating status'.format(str(r['staging_id'])))
                update_status(r['id'],'Staged') ## don't reset the staging id till download happens
            else:
                print('Staging for {:s} is {:s} (staging id {:s})'.format(field,stage_status,str(s)))

    ## this does one download at a time
    if ksum<totallimit and 'Staged' in d and download_thread is None:
        download_name=fd['Staged'][0]
        print('We need to download a new file (%s)!' % download_name)
        ## probably want to pass the staging id here
        download_thread=threading.Thread(target=do_download, args=(download_name,))
        download_thread.start()

    ## unpacking the files
    if 'Downloaded' in d and unpack_thread is None:
        unpack_name=fd['Downloaded'][0]
        print('We need to unpack a new file (%s)!' % unpack_name)
        unpack_thread=threading.Thread(target=do_unpack, args=(unpack_name,))
        unpack_thread.start()

    # if 'Unpacked' in d:
    #     if 'Queued' in d:
    #         nq = d['Queued']
    #     else:
    #         nq = 0
    #     for field in fd['Unpacked']:
    #         if nq <= maxqueue:
    #             nq = nq + 1
    #             print('Running a new job',field)
    #             update_status(field,'Queued')
    #             ### will need to change the script
    #             command="sbatch -J %s %s/slurm/run_linc_calibrator.sh %s" % (field, str(basedir).rstrip('/'), field)
    #             if os.system(command):
    #                 update_status(field,"Submission failed")
    #         else:
    #             print( 'Queue is full, {:s} waiting for submission'.format(field) )

    # if 'Queued' in d:
    #     for field in fd['Queued']:
    #         print('Verifying processing for',field)
    #         outdir = os.path.join(procdir,field)
    #         if os.path.isfile(os.path.join(outdir,'finished.txt')):        
    #             result = check_field(field)
    #             if result:
    #                 update_status(field,'Verified')
    #             else:
    #                 update_status(field,'Workflow failed')

    # ## this will also need to be changed to use macaroons to copy back to spider
    # if 'Verified' in d and verify_thread is None:
    #     verify_name = fd['Verified'][0]
    #     verify_thread=threading.Thread(target=do_verify, args=(verify_name,))
    #     verify_thread.start()


    print('\n\n-----------------------------------------------\n\n')
    
    sleep(60)
