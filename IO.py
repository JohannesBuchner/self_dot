#!/usr/bin/python
# -*- coding: latin-1 -*-

import multiprocessing as mp
import os
import random

import cv2
import numpy as np
import zmq

from utils import send_array, recv_array
import myCsoundAudioOptions

# �MQ ports
CAMERA = 5561
PROJECTOR = 5562
MIC = 5563
SPEAKER = 5564
STATE = 5565
EXTERNAL = 5566
SNAPSHOT = 5567
EVENT = 5568
ASSOCIATIONS = 5569
ROBO = 5570
#ROBOBACK = 5571
FACE = 5572

def video():
    me = mp.current_process()
    print me.name, 'PID', me.pid

    #cv2.namedWindow('Output', cv2.WINDOW_NORMAL)

    cv2.namedWindow('Output', cv2.WND_PROP_FULLSCREEN)
    camera = cv2.VideoCapture(0)

    context = zmq.Context()
    publisher = context.socket(zmq.PUB)
    publisher.bind('tcp://*:{}'.format(CAMERA))

    subscriber = context.socket(zmq.PULL)
    subscriber.bind('tcp://*:{}'.format(PROJECTOR))
    
    frame_size = (640,360)

    stateQ = context.socket(zmq.SUB)
    stateQ.connect('tcp://localhost:{}'.format(STATE))
    stateQ.setsockopt(zmq.SUBSCRIBE, b'') 
    poller = zmq.Poller()
    poller.register(stateQ, zmq.POLLIN)

    while True:
        events = dict(poller.poll(timeout=0))
        if stateQ in events:
            state = stateQ.recv_json()        

            if state['fullscreen'] > 0:
                cv2.setWindowProperty('Output', cv2.WND_PROP_FULLSCREEN, cv2.cv.CV_WINDOW_FULLSCREEN)
                state['fullscreen'] = 0
            if state['display2'] > 0:
                cv2.moveWindow('Output', 2100, 100)
                state['display2'] = 0

        _, frame = camera.read()
        frame = cv2.resize(frame, frame_size)
        send_array(publisher, frame)
        
        try: 
            cv2.imshow('Output', cv2.resize(recv_array(subscriber, flags=zmq.DONTWAIT), frame_size))
        except:
            cv2.imshow('Output', np.zeros(frame_size[::-1]))

        cv2.waitKey(100)

def audio():
    me = mp.current_process()
    print me.name, 'PID', me.pid

    context = zmq.Context()
    publisher = context.socket(zmq.PUB)
    publisher.bind('tcp://*:{}'.format(MIC))

    assoc = context.socket(zmq.PUB)
    assoc.bind('tcp://*:{}'.format(ASSOCIATIONS))

    robocontrol = context.socket(zmq.PUB)
    robocontrol.bind('tcp://*:{}'.format(ROBO))

    #roboback = context.socket(zmq.SUB)
    #roboback.connect('tcp://localhost:{}'.format(ROBOBACK))
    #roboback.setsockopt(zmq.SUBSCRIBE, b'')

    subscriber = context.socket(zmq.PULL)
    subscriber.bind('tcp://*:{}'.format(SPEAKER))

    stateQ = context.socket(zmq.SUB)
    stateQ.connect('tcp://localhost:{}'.format(STATE))
    stateQ.setsockopt(zmq.SUBSCRIBE, b'') 

    eventQ = context.socket(zmq.SUB)
    eventQ.connect('tcp://localhost:{}'.format(EVENT))
    eventQ.setsockopt(zmq.SUBSCRIBE, b'') 

    snapshot = context.socket(zmq.REQ)
    snapshot.connect('tcp://localhost:{}'.format(SNAPSHOT))
    snapshot.send(b'Send me the state, please')
    state = snapshot.recv_json()

    poller = zmq.Poller()
    poller.register(subscriber, zmq.POLLIN)
    poller.register(stateQ, zmq.POLLIN)
    poller.register(eventQ, zmq.POLLIN)
    poller.register(assoc, zmq.POLLIN)
    #poller.register(roboback, zmq.POLLIN)

    import time
    t_str = time.strftime
    t_tim = time.time()

    memRecPath = "./memory_recordings/"

    if not os.path.exists(memRecPath):
        os.makedirs(memRecPath)
        
    import csnd6
    cs = csnd6.Csound()
    arguments = csnd6.CsoundArgVList()
    arguments.Append("dummy")
    arguments.Append("self_dot.csd")
    csoundCommandline = myCsoundAudioOptions.myAudioDevices
    comlineParmsList = csoundCommandline.split(' ')
    for item in comlineParmsList:
        arguments.Append("%s"%item)
    cs.Compile(arguments.argc(), arguments.argv())
    stopflag = 0
    zeroChannelsOnNoBrain = 1
    
    fftsize = int(cs.GetChannel("fftsize"))
    ffttabsize = fftsize/2
    fftin_amptab = 1
    fftin_freqtab = 2
    fftout_amptab = 4
    fftout_freqtab = 5
    fftresyn_amptab = 7
    fftresyn_freqtab = 8
    
    # optimizations to avoid function lookup inside loop
    tGet = cs.TableGet 
    tSet = cs.TableSet
    cGet = cs.GetChannel
    cSet = cs.SetChannel
    perfKsmps = cs.PerformKsmps
    fftbinindices = range(ffttabsize)
    fftin_amptabs = [fftin_amptab]*ffttabsize
    fftin_freqtabs = [fftin_freqtab]*ffttabsize
    fftout_amptabs = [fftout_amptab]*ffttabsize
    fftout_freqtabs = [fftout_freqtab]*ffttabsize
    fftresyn_amptabs = [fftresyn_amptab]*ffttabsize
    fftresyn_freqtabs = [fftresyn_freqtab]*ffttabsize
    fftzeros = [0]*ffttabsize
    fftconst = [0.1]*ffttabsize
    fftin_amplist = [0]*ffttabsize
    fftin_freqlist = [0]*ffttabsize

    filename = []
    counter = 0
    while not stopflag:
        counter += 1
        stopflag = perfKsmps()
        fftinFlag = cGet("pvsinflag")
        fftoutFlag = cGet("pvsoutflag")
        
        if fftinFlag:
            fftin_amplist = map(tGet,fftin_amptabs,fftbinindices)
            fftin_freqlist = map(tGet,fftin_freqtabs,fftbinindices)
            #bogusamp = map(tSet,fftresyn_amptabs,fftbinindices,fftin_amplist)
            #bogusfreq = map(tSet,fftresyn_freqtabs,fftbinindices,fftin_freqlist)
        if fftoutFlag:
            fftout_amplist = map(tGet,fftout_amptabs,fftbinindices)
            fftout_freqlist = map(tGet,fftout_freqtabs,fftbinindices)

        events = dict(poller.poll(timeout=0))

        if stateQ in events:
            state = stateQ.recv_json()

        # get Csound channel data
        audioStatus = cGet("audioStatus")           
        audioStatusTrig = cGet("audioStatusTrig")       # signals start of a statement (audio in)
        transient = cGet("transient")                   # signals start of a segment within a statement (audio in)        
        memRecTimeMarker = cGet("memRecTimeMarker")     # (in memRec) get the time since start of statement
        memRecActive = cGet("memRecActive")             # flag to check if memoryRecording is currently recording to file in Csound
        memRecMaxAmp = cGet("memRecMaxAmp")             # max amplitude for each recorded file
        panposition = cs.GetChannel("panalyzer_pan")

        if state['roboActive'] > 0:
            if panposition != 0.5:
                robocontrol.send_json([1,'pan',panposition])
            if (counter % 500) == 0:
                robocontrol.send_json([2,'pan',-1])
         
        if state['memoryRecording']:
            if audioStatusTrig > 0:
                print 'starting memoryRec'
                timestr = t_str('%Y_%m_%d_%H_%M_%S')
                tim_time = t_tim
                filename = memRecPath+timestr+'.wav'
                cs.InputMessage('i 34 0 -1 "%s"'%filename)
                markerfileName = memRecPath+timestr+'.txt'
                markerfile = open(markerfileName, 'w')
                markerfile.write('Self. audio clip perceived at %s\n'%tim_time)
                segments = 'Sub segment start times: \n0.000 \n'
            if (transient > 0) & (memRecActive > 0):
                segments += '%.3f \n'%memRecTimeMarker
            if (audioStatusTrig < 0) & (memRecActive > 0):
                cs.InputMessage('i -34 0 1')
                markerfile.write(segments)
                markerfile.write('Total duration: %f\n'%memRecTimeMarker)
                markerfile.write('\nMax amp for file: %f'%memRecMaxAmp)
                markerfile.close()
                print 'stopping memoryRec'
                assoc.send_json(markerfileName)

        if not state['memoryRecording'] and memRecActive:
            cs.InputMessage('i -34 0 1')
            markerfile.write(segments)
            markerfile.write('Total duration: %f'%memRecTimeMarker)
            markerfile.close()
            print 'stopping memoryRec'
            assoc.send_json(markerfileName)
                                
        if state['autolearn']:
            if audioStatusTrig > 0:
                send('startrec', context)
            if audioStatusTrig < 0:
                send('stoprec', context)
                if filename:
                    send('learnwav {}'.format(os.path.abspath(filename)), context)

        if state['autorespond']:
            if audioStatusTrig > 0:
                send('startrec', context)
            if audioStatusTrig < 0:
                send('stoprec', context)
                if filename:
                    send('respondwav {}'.format(os.path.abspath(filename)), context) 

        if eventQ in events:
            pushbutton = eventQ.recv_json()
            if 'selfvoice' in pushbutton:
                    print 'not implemented'

            if 'inputLevel' in pushbutton:
                mode = pushbutton['inputLevel']
                if mode == 'mute':
                    cs.InputMessage('i 21 0 .1 0')
                    print 'Mute'
                if mode == 'unmute':
                    cs.InputMessage('i 21 0 .1 1')
                    print 'Un-mute'
                if mode == 'reset': 
                    cs.InputMessage('i 21 0 .1 0')
                    cs.InputMessage('i 21 1 .1 1')

            if 'calibrateAudio' in pushbutton:
                cs.InputMessage('i -17 0 1') # turn off old noise gate
                cs.InputMessage('i 12 0 4') # measure roundtrip latency
                cs.InputMessage('i 13 4 1.9') # get audio input noise print
                cs.InputMessage('i 14 6 -1 1.0 1.0') # enable noiseprint and self-output suppression
                cs.InputMessage('i 15 6.2 2') # get noise floor level 
                cs.InputMessage('i 16 8.3 0.1') # set noise gate shape
                cs.InputMessage('i 17 8.5 -1') # turn on new noise gate

            if 'csinstr' in pushbutton:
                # generic csound instr message
                cs.InputMessage('{}'.format(pushbutton['csinstr']))
                print 'sent {}'.format(pushbutton['csinstr'])

            if 'selfDucking' in pushbutton:
                value = pushbutton['selfDucking']
                cs.InputMessage('i 22 0 1 "selfDucking" %f'%float(value))

            if 'zerochannels' in pushbutton:
                zeroChannelsOnNoBrain = int('{}'.format(pushbutton['zerochannels']))

            if 'playfile' in pushbutton:
                print '[self.] playfile {}'.format(pushbutton['playfile'])
                try:
                    params = pushbutton['playfile']
                    soundfile, maxamp = params.split(' ')
                    soundfile = str(soundfile)
                    voiceChannel = random.choice([1,2]) # internal or external voice (primary/secondary associations)
                    voiceType = random.choice([1,2,3,4,5,6]) # different voice timbres, (0-7), see self_voices.inc for details
                    instr = 60 + voiceType
                    start = 0 # segment start and end within sound file
                    end = 0 # if zero, play whole file
                    amp = -3 # voice amplitude in dB
                    if voiceChannel == 2:
                        delaySend = -26 # delay send in dB
                        reverbSend = -23 # reverb send in dB
                    else:
                        delaySend = -96
                        reverbSend = -35 
                    if voiceType == 7:
                        speed = 0.6 #playback  speed
                    else:
                        speed = 1 
                    #cs.InputMessage('i 6 0 .1 440')                    
                    csMessage = 'i %i 0 1 "%s" %f %f %f %f %i %f %f %f' %(instr, soundfile, start, end, amp, float(maxamp), voiceChannel, delaySend, reverbSend, speed)
                    #csMessage = 'i %i 0 1 "%s" %f %f %f' %(instr, soundfile, start, end, amp)
                    print 'csMessage', csMessage                 
                    cs.InputMessage(csMessage)

                except Exception, e:
                    print e, 'Playfile aborted.'

# Setup so it can be accessed from processes which don't have a zmq context, i.e. for one-shot messaging
def send(message, context=None, host='localhost', port=EXTERNAL):
    context = context or zmq.Context()
    sender = context.socket(zmq.PUSH)
    sender.connect('tcp://{}:{}'.format(host, port))
    sender.send_json(message)
