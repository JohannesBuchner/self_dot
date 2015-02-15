#!/usr/bin/python
# -*- coding: latin-1 -*-

import multiprocessing as mp
from uuid import uuid4
from collections import deque
import sys
import glob
import cPickle as pickle
from subprocess import call
import time
import os
import itertools
from collections import namedtuple
import random

import numpy as np
import zmq
from sklearn import preprocessing as pp
from sklearn import svm
from scipy.io import wavfile
from scikits.samplerate import resample
from sklearn.decomposition import RandomizedPCA
from scipy.signal.signaltools import correlate2d as c2d
import sai as pysai
from zmq.utils.jsonapi import dumps
from scipy.stats import itemfreq
from scipy.cluster.vq import kmeans, vq

import utils
import IO
import association
import my_sai_test as mysai
import myCsoundAudioOptions
from pykalman import KalmanFilter

try:
    opencv_prefix = os.environ['VIRTUAL_ENV']
except:
    opencv_prefix = '/usr/local'
    print 'VIRTUAL_ENV variable not set, we are guessing OpenCV files reside in /usr/local - if OpenCV croaks, this is the reason.'
    
FACE_HAAR_CASCADE_PATH = opencv_prefix + '/share/OpenCV/haarcascades/haarcascade_frontalface_default.xml'
EYE_HAAR_CASCADE_PATH = opencv_prefix + '/share/OpenCV/haarcascades/haarcascade_eye_tree_eyeglasses.xml'

# Hamming distance match criterions
AUDIO_HAMMERTIME = 9
RHYME_HAMMERTIME = 11
FACE_HAMMERTIME = 15
FRAME_SIZE = (160,120) # Neural network image size, 1/4 of full frame size.
FACE_MEMORY_SIZE = 100
AUDIO_MEMORY_SIZE = 500
MAX_CATEGORY_SIZE = 10
PROTECT_PERCENTAGE = .2
VIDEO_PERCENT_LENGTH = .8

NUMBER_OF_BRAINS = 5

AudioVisualSegment = namedtuple('AudioVisualSegment', ['audio_segment', 'face_id', 'video_esn'])
VisualSegment = namedtuple('VisualSegment', 'face_id, video_esn')

class AudioVisualMemory:
    def __init__(self):
        self.memory = {}

    def learn(self, audio_segment, face_id, video_esn):
        self.memory[audio_segment] = VisualSegment(face_id, video_esn)

    def project(self, audio_segment, NAP):
        video_esn = utils.load_esn(self.memory[audio_segment].video_esn)
        stride = IO.VIDEO_SAMPLE_TIME / (IO.NAP_RATE/IO.NAP_STRIDE)
        length = np.floor(NAP.shape[0]*VIDEO_PERCENT_LENGTH).astype('int')
        NAP = NAP[:length:stride]
        projection = video_esn(NAP)
        projection_filename = '{}_PROJECTION_{}'.format(audio_segment.wav_file, uuid4())
        np.save(projection_filename, projection)
        return projection_filename

    # LEGACY CRUFT FOR ASSOCIATION
    @property 
    def wavs(self):
        _wavs = []
        for audio_segment in sorted(self.memory.keys(), key = lambda x: x.audio_id):
            try:
                _wavs[audio_segment.audio_id].append(audio_segment.wav_file)
            except:
                _wavs.append([audio_segment.wav_file])
        return _wavs

    # LEGACY CRUFT FOR ASSOCIATION
    @property
    def wav_audio_ids(self):
        _wav_audio_ids = {}
        for audio_segment in self.memory.keys():
            _wav_audio_ids[(audio_segment.wav_file, audio_segment.audio_id)] = [ audio_segment.start, audio_segment.end ]
        return _wav_audio_ids

_AudioSegment = namedtuple('_AudioSegment', ['audio_id', 'crude_hash', 'fine_hash', 'wav_file', 'start', 'end', 'normalized_start', 'normalized_end'])

class AudioSegment(_AudioSegment):
    @property
    def duration(self):
        return self.end - self.start

class AudioMemory:
    def __init__(self):
        self.num_bins = 11
        self.clean = np.linspace(0, 200, self.num_bins) # Effectively 10 second limit, .5 second overlaps.
        self.overlap = np.linspace(10, 210, self.num_bins)
        self.NAP_intervals = {}
        self.audio_ids = {}
        self.audio_id_counter = 0

    def _digitize(self, NAP):
        idx_clean = np.digitize([ NAP.shape[0] ], self.clean)[0]
        idx_overlap = np.digitize([ NAP.shape[0] ], self.overlap)[0]

        clean_key = (self.clean[idx_clean - 1], self.clean[min(self.num_bins-1,idx_clean)])
        overlap_key = (self.overlap[idx_overlap - 1], self.overlap[min(self.num_bins-1,idx_overlap)])

        return clean_key, overlap_key

    def find(self, NAP):
        crude_hash = utils.d_hash(NAP, hash_size=8)
        fine_hash = utils.d_hash(NAP, hash_size=16)
        clean_key, overlap_key = self._digitize(NAP)

        unsorted = self.NAP_intervals[clean_key] if clean_key in self.NAP_intervals else [] + self.NAP_intervals[overlap_key] if overlap_key in self.NAP_intervals else []

        return sorted(unsorted, key = lambda x: utils.hamming_distance(fine_hash, x.fine_hash))[0] if len(unsorted) else [], crude_hash, fine_hash, clean_key, overlap_key
    
    def learn(self, NAP, wav_file, segstart, segend, total_duration):
        best_match, crude_hash, fine_hash, clean_key, overlap_key = self.find(NAP)

        # This is where the logging of the audio_id counter should take place.
        
        if not len(best_match):
            audio_id = self.audio_id_counter
            print 'New audio_id {}, never before heard length {}'.format(audio_id, NAP.shape[0])
            self.audio_id_counter += 1
        else:
            if utils.hamming_distance(crude_hash, best_match.crude_hash) < AUDIO_HAMMERTIME:
                audio_id = best_match.audio_id
                print 'Similar to audio_id {}, hamming distance {}'.format(audio_id, utils.hamming_distance(crude_hash, best_match.crude_hash))
            else:
                audio_id = self.audio_id_counter
                self.audio_id_counter += 1                
                print 'New audio_id {}, hamming distance {} from audio_id {}'.format(audio_id, utils.hamming_distance(crude_hash, best_match.crude_hash), best_match.audio_id)
                
        audio_segment = AudioSegment(audio_id, crude_hash, fine_hash, wav_file, segstart, segend, segstart/total_duration, segend/total_duration)

        utils.insert(self.NAP_intervals, clean_key, audio_segment)
        utils.insert(self.NAP_intervals, overlap_key, audio_segment)
        utils.insert(self.audio_ids, audio_id, audio_segment)

        if len(self.audio_ids[audio_id]) > MAX_CATEGORY_SIZE:
            self.forget(self.audio_ids[audio_id][0])
            print 'Forgetting oldest member of audio_id {}, size after deletion: '.format(audio_id, len(self.audio_ids[audio_id]))

        # Check to see if we have too many memories, delete the oldest least used.

        return audio_segment

    def all_segments(self):
        return [ audio_segment for key, value in self.audio_ids.iteritems() for audio_segment in value ]

    def forget(self, audio_segment):
        self.audio_ids[audio_segment.audio_id].remove(audio_segment)

        for _, audio_segments in self.NAP_intervals.iteritems():
            try:
                audio_segments.remove(audio_segment)
            except:
                continue
        self._cleanse_keys()
        
    def _cleanse_keys(self):
        for empty_key in [ key for key, value in self.audio_ids.iteritems() if len(value) == 0 ]:
            del self.audio_ids[empty_key]
        for empty_key in [ key for key, value in self.NAP_intervals.iteritems() if len(value) == 0 ]:
            del self.NAP_intervals[empty_key]


def respond(control_host, learn_host, debug=False):
    context = zmq.Context()
    
    eventQ = context.socket(zmq.SUB)
    eventQ.connect('tcp://{}:{}'.format(control_host, IO.EVENT))
    eventQ.setsockopt(zmq.SUBSCRIBE, b'') 

    projector = context.socket(zmq.PUSH)
    projector.connect('tcp://{}:{}'.format(control_host, IO.PROJECTOR)) 

    sender = context.socket(zmq.PUSH)
    sender.connect('tcp://{}:{}'.format(control_host, IO.EXTERNAL))

    brainQ = context.socket(zmq.PULL)
    brainQ.bind('tcp://*:{}'.format(IO.BRAIN))

    counterQ = context.socket(zmq.REQ)
    counterQ.connect('tcp://{}:{}'.format(control_host, IO.COUNTER))
    
    cognitionQ = context.socket(zmq.PUSH)
    cognitionQ.connect('tcp://{}:{}'.format(control_host, IO.COGNITION))

    association = context.socket(zmq.REQ)
    association.connect('tcp://{}:{}'.format(learn_host, IO.ASSOCIATION))

    snapshot = context.socket(zmq.REQ)
    snapshot.connect('tcp://{}:{}'.format(control_host, IO.SNAPSHOT))

    scheduler = context.socket(zmq.PUSH)
    scheduler.connect('tcp://{}:{}'.format(control_host, IO.SCHEDULER))

    dreamQ = context.socket(zmq.PULL)
    dreamQ.bind('tcp://*:{}'.format(IO.DREAM))

    snapshot.send_json('Give me state!')
    state = snapshot.recv_json()

    poller = zmq.Poller()
    poller.register(eventQ, zmq.POLLIN)
    poller.register(brainQ, zmq.POLLIN)
    poller.register(dreamQ, zmq.POLLIN)

    sound_to_face = []
    wordFace = {}
    face_to_sound = []
    faceWord = {}
    register = {}
    video_producer = {}
    voiceType1 = 1
    voiceType2 = 6
    wordSpace1 = 0.3
    wordSpaceDev1 = 0.3
    wordSpace2 = 0.1
    wordSpaceDev2 = 0.3

    audio_ids = []
    wavs = []
    wav_audio_ids = []
    NAP_hashes = {}
    most_significant_audio_id = []

    AV_memory = AudioVisualMemory()
    
    if debug:
        import matplotlib.pyplot as plt
        plt.ion()
    
    while True:
        events = dict(poller.poll())

        if brainQ in events:
            cells = brainQ.recv_pyobj()

            mode = cells[0]
            wav_file = cells[1]

            if wav_file not in register:
                register[wav_file] = [False, False, False]

            if mode == 'audio_learn':
                register[wav_file][0] = cells
                            
            if mode == 'video_learn':
                register[wav_file][1] = cells

            if mode == 'face_learn':
                register[wav_file][2] = cells

            if all(register[wav_file]):
                _, _, audio_segments, audio_memory, most_significant_audio_id = register[wav_file][0]
                _, _, tarantino = register[wav_file][1]
                _, _, face_id, face_recognizer = register[wav_file][2]          
                print 'Audio - video - face recognizers related to {} arrived at responder, total processing time {} seconds'.format(wav_file, time.time() - utils.filetime(wav_file))

                for audio_segment in audio_segments:
                    AV_memory.learn(audio_segment, face_id, tarantino)

                    wordFace.setdefault(audio_segment.audio_id, [[face_id,0]])
                    found = 0
                    for item in wordFace[audio_segment.audio_id]:
                        if item[0] == face_id:
                            item[1] += 1
                            found = 1
                    if found == 0:
                        wordFace[audio_segment.audio_id].append([face_id,1])

                    # We can't go from a not known face to any of the sounds.
                    print 'face_id for audio segment learned', face_id
                    if face_id is not -1:
                        faceWord.setdefault(face_id, [[audio_segment.audio_id,0]])
                        found = 0
                        for item in faceWord[face_id]:
                            if item[0] == audio_segment.audio_id:
                                item[1] += 1
                                found = 1
                        if found == 0:
                            faceWord[face_id].append([audio_segment.audio_id,1])
                            
                del register[wav_file]
                
                similar_ids = []
                for audio_segment in audio_segments:
                    similar_ids.append([ utils.hamming_distance(audio_segment.crude_hash, random.choice(h).crude_hash) for h in audio_memory.audio_ids.itervalues() ])

                if len(audio_segments):
                    association.send_pyobj(['analyze', wav_file, AV_memory.wav_audio_ids, [ audio_segment.audio_id for audio_segment in audio_segments ], AV_memory.wavs, similar_ids,wordFace,faceWord])
                    association.recv_pyobj()
                    sender.send_json('last_most_significant_audio_id {}'.format(most_significant_audio_id))

                cognitionQ.send_pyobj(face_recognizer) # A possiblity of recognizing a face that is not connecting to any soundfiles

                                
        if eventQ in events:
            pushbutton = eventQ.recv_json()
            if 'respond_single' in pushbutton:
                try:
                    filename = pushbutton['filename']
                    audio_segments = utils.get_segments(filename)
                    print 'Echo response to {} duration {} seconds with {} segments'.format(filename, audio_segments[-1], len(audio_segments)-1)
                    new_sentence = utils.csv_to_array(filename + 'cochlear')
                    norm_segments = np.rint(new_sentence.shape[0]*audio_segments/audio_segments[-1]).astype('int')
                    
                    play_events = []
                    for NAP in [ utils.trim_right(new_sentence[norm_segments[i]:norm_segments[i+1]]) for i in range(len(norm_segments)-1) ]:
                        best_match,_,_,_,_ = audio_memory.find(NAP)
                        
                        print 'Recognized as sound {}'.format(best_match.audio_id)

                        voiceChannel = 1
                        speed = 1
                        amp = -3
                        _,dur,maxamp,_ = utils.getSoundInfo(best_match.wav_file)

                        start = 0
                        voice1 = 'playfile {} {} {} {} {} {} {} {} {}'.format(1, voiceType1, start, best_match.wav_file, speed, best_match.start, best_match.end, amp, maxamp)
                        voice2 = 'playfile {} {} {} {} {} {} {} {} {}'.format(2, voiceType1, start, best_match.wav_file, speed, best_match.start, best_match.end, amp, maxamp)

                        projection = AV_memory.project(best_match, NAP)
                        play_events.append([ dur+.1, voice1, voice2, projection, FRAME_SIZE ])

                    scheduler.send_pyobj(play_events)
                    print 'Respond time from creation of wav file was {} seconds'.format(time.time() - utils.filetime(filename))
                except:
                    utils.print_exception('Echo response aborted.')

            # if 'play_sentence' in pushbutton:
            #     try:
            #         sentence = pushbutton['sentence']
            #         sentence = eval(sentence)
            #         print '*** (play) Play sentence', sentence
            #         start = 0 
            #         nextTime1 = 0
            #         play_events = []
            #         for i in range(len(sentence)):
            #             word_id = sentence[i]
            #             soundfile = np.random.choice(wavs[word_id])
            #             speed = 1

            #             segstart, segend = wav_audio_ids[(soundfile, word_id)]
            #             NAP = _extract_NAP(segstart, segend, soundfile)

            #             amp = -3 # voice amplitude in dB
            #             _,totaldur,maxamp,_ = utils.getSoundInfo(soundfile)
            #             dur = segend-segstart
            #             if dur <= 0: dur = totaldur
            #             # play in both voices
            #             voice1 = 'playfile {} {} {} {} {} {} {} {} {}'.format(1, voiceType1, start, soundfile, speed, segstart, segend, amp, maxamp)
            #             voice2 = 'playfile {} {} {} {} {} {} {} {} {}'.format(2, voiceType1, start, soundfile, speed, segstart, segend, amp, maxamp)
            #             wordSpacing1 = wordSpace1 + np.random.random()*wordSpaceDev1
            #             print 'PLAY RESPOND SPACING', wordSpacing1
            #             nextTime1 += (dur/speed)+wordSpacing1

            #             projection = _project(audio_id, sound_to_face, NAP, video_producer)

            #             play_events.append([ dur+wordSpacing1, voice1, voice2, projection, FRAME_SIZE ])                        
            #         scheduler.send_pyobj(play_events)
            #     except:
            #         utils.print_exception('Sentence play aborted.')

            if 'respond_sentence' in pushbutton:
                print 'SENTENCE Respond to', pushbutton['filename'][-12:]
                    
                try:
                    filename = pushbutton['filename']
                    audio_segments = utils.get_segments(filename)
                    print 'Sentence response to {} duration {} seconds with {} segments'.format(filename, audio_segments[-1], len(audio_segments)-1)
                    new_sentence = utils.csv_to_array(filename + 'cochlear')
                    norm_segments = np.rint(new_sentence.shape[0]*audio_segments/audio_segments[-1]).astype('int')

                    segment_id = utils.get_most_significant_word(filename)
                    print '**Sentence selected to respond to segment {}'.format(segment_id)

                    NAP = utils.trim_right(new_sentence[norm_segments[segment_id]:norm_segments[segment_id+1]])

                    best_match,_,_,_,_ = audio_memory.find(NAP)
        
                    numWords = len(audio_segments)-1
                    print numWords
                    association.send_pyobj(['setParam', 'numWords', numWords ])
                    association.recv_pyobj()
                    
                    association.send_pyobj(['makeSentence', best_match.audio_id])
                    print 'respond_sentence waiting for association output...', 
                    sentence, secondaryStream = association.recv_pyobj()

                    print '*** (respond) Play sentence', sentence, secondaryStream
                    start = 0 
                    nextTime1 = 0
                    nextTime2 = 0
                    enableVoice2 = 1

                    play_events = []

                    for i in range(len(sentence)):
                        word_id = sentence[i]
                        audio_segment = random.choice(audio_memory.audio_ids[word_id])
                        voiceChannel = 1
                        speed = 1
                        
                        NAP = _extract_NAP(audio_segment)
                        
                        amp = -3 # voice amplitude in dB
                        _,totaldur,maxamp,_ = utils.getSoundInfo(audio_segment.wav_file)
                        dur = audio_segment.duration
                        if dur <= 0: dur = totaldur
                        voice1 = 'playfile {} {} {} {} {} {} {} {} {}'.format(voiceChannel, voiceType1, start, audio_segment.wav_file, speed, audio_segment.start, audio_segment.end, amp, maxamp)
                        #start += dur # if we want to create a 'score section' for Csound, update start time to make segments into a contiguous sentence
                        wordSpacing1 = wordSpace1 + np.random.random()*wordSpaceDev1
                        nextTime1 += (dur/speed)+wordSpacing1
                        #print 'voice 2 ready to play', secondaryStream[i], i
                        if enableVoice2:
                            word_id2 = secondaryStream[i]
                            #print 'voice 2 playing', secondaryStream[i]
                            audio_segment2 = random.choice(audio_memory.audio_ids[word_id2])
                            voiceChannel2 = 2
                            start2 = 0.7 #  set delay between voice 1 and 2
                            speed2 = 0.7
                            amp2 = -10 # voice amplitude in dB
                            dur2 = audio_segment2.duration
                            #totalDur2, maxamp2 = utils.getSoundParmFromFile(soundfile2)
                            _,totalDur2,maxamp2,_ = utils.getSoundInfo(audio_segment2.wav_file)
                            if dur2 <= 0: dur2 = totalDur2
                            voice2 = 'playfile {} {} {} {} {} {} {} {} {}'.format(voiceChannel2, voiceType2, start2, audio_segment2.wav_file, speed2, audio_segment2.start, audio_segment2.end, amp2, maxamp2)
                            wordSpacing2 = wordSpace2 + np.random.random()*wordSpaceDev2
                            nextTime2 += (dur2/speed2)+wordSpacing2
                            #enableVoice2 = 0
                        # trig another word in voice 2 only if word 2 has finished playing (and sync to start of voice 1)
                        if nextTime1 > nextTime2: enableVoice2 = 1 

                        projection = AV_memory.project(audio_segment, NAP)
                        print 'SENTENCE RESPOND SPACING', wordSpacing1
                        play_events.append([ dur+wordSpacing1, voice1, voice2, projection, FRAME_SIZE ])

                    scheduler.send_pyobj(play_events)
                    print 'Sentence respond time from creation of wav file was {} seconds'.format(time.time() - utils.filetime(filename))
                except:
                    utils.print_exception('Sentence response aborted.')
                    
            if 'testSentence' in pushbutton:
                print 'testSentence', pushbutton
                association.send_pyobj(['makeSentence',int(pushbutton['testSentence'])])
                print 'testSentence waiting for association output...'
                sentence, secondaryStream = association.recv_pyobj()
                print '*** Test sentence', sentence, secondaryStream
            
            if 'assoc_setParam' in pushbutton:
                try:
                    parm, value = pushbutton['assoc_setParam'].split()
                    association.send_pyobj(['setParam', parm, value ])
                    association.recv_pyobj()
                except:
                    utils.print_exception('Assoc set param aborted.')

            if 'respond_setParam' in pushbutton:
                items = pushbutton['respond_setParam'].split()
                if items[0] == 'voiceType':
                    chan = items[1]
                    if chan == '1': voiceType1 = int(items[2])
                    if chan == '2': voiceType2 = int(items[2])
                if items[0] == 'wordSpace':
                    chan = items[1]
                    print 'wordSpace chan', chan, items
                    if chan == '1': wordSpace1 = float(items[2])
                    if chan == '2': wordSpace2 = float(items[2])
                if items[0] == 'wordSpaceDev':
                    chan = items[1]
                    print 'wordSpaceDev1 chan', chan, items
                    if chan == '1': wordSpaceDev1 = float(items[2])
                    if chan == '2': wordSpaceDev2 = float(items[2])

            if 'play_id' in pushbutton:
                try:
                    items = pushbutton['play_id'].split(' ')
                    if len(items) < 3: print 'PARAMETER ERROR: play_id audio_id voiceChannel voiceType'
                    play_audio_id = int(items[0])
                    voiceChannel = int(items[1])
                    voiceType = int(items[2])
                    print 'play_audio_id', play_audio_id, 'voice', voiceChannel
                    print 'wavs[play_audio_id]', wavs[play_audio_id]
                    #print wavs
                    soundfile = np.random.choice(wavs[play_audio_id])
                    
                    speed = 1
                    #print 'wav_audio_ids', wav_audio_ids
                    segstart, segend = wav_audio_ids[(soundfile, play_audio_id)]
                    #segstart = 0 # segment start and end within sound file
                    #segend = 0 # if zero, play whole file
                    amp = -3 # voice amplitude in dB
                    #dur, maxamp = utils.getSoundParmFromFile(soundfile)
                    _,dur,maxamp,_ = utils.getSoundInfo(soundfile)
                    start = 0
                    sender.send_json('playfile {} {} {} {} {} {} {} {} {}'.format(voiceChannel, voiceType, start, soundfile, speed, segstart, segend, amp, maxamp))
                except:
                    utils.print_exception('play_id aborted.')

            if 'print_me' in pushbutton:
                # just for inspecting the contents of objects while running 
                print 'printing '+pushbutton['print_me']
                if 'brain ' in pushbutton['print_me']: 
                    print_variable = pushbutton['print_me'].split('brain ')[-1]
                    try:
                        print eval(print_variable)
                    except Exception, e:
                        print e, 'print_me in brain failed.'
                elif 'association ' in pushbutton['print_me']: 
                    print_variable = pushbutton['print_me'].split('association ')[-1]
                    association.send_pyobj(['print_me',print_variable])

            if 'dream' in pushbutton:
                play_events = []
                for audio_segment in audio_memory.all_segments():
                    NAP = _extract_NAP(audio_segment)
                    speed = 1
                    amp = -3
                    maxamp = 1
                    start = 0
                    voice1 = 'playfile {} {} {} {} {} {} {} {} {}'.format(1, 6, np.random.rand()/3, audio_segment.wav_file, speed, audio_segment.start, audio_segment.end, amp, maxamp)
                    projection = AV_memory.project(audio_segment, NAP)
                    voice2 = 'playfile {} {} {} {} {} {} {} {} {}'.format(2, 6, np.random.randint(3,6), audio_segment.wav_file, speed, audio_segemtn.start, audio_segment.end, amp, maxamp)
                    play_events.append([ audio_segment.duration, voice1, voice2, projection, FRAME_SIZE ])
                print 'Dream mode playing back {} memories'.format(len(play_events))
                scheduler.send_pyobj(play_events)

            if 'save' in pushbutton:
                utils.save('{}.{}'.format(pushbutton['save'], mp.current_process().name), [ sound_to_face, wordFace, face_to_sound, faceWord, video_producer, wavs, wav_audio_ids, NAP_hashes, face_id, face_recognizer, audio_memory, AV_memory ])

            if 'load' in pushbutton:
                sound_to_face, wordFace, face_to_sound, faceWord, video_producer, wavs, wav_audio_ids, NAP_hashes, face_id, face_recognizer, audio_memory, AV_memory = utils.load('{}.{}'.format(pushbutton['load'], mp.current_process().name))


def learn_audio(host, debug=False):
    context = zmq.Context()

    mic = context.socket(zmq.SUB)
    mic.connect('tcp://{}:{}'.format(host, IO.MIC))
    mic.setsockopt(zmq.SUBSCRIBE, b'')

    dreamQ = context.socket(zmq.PUSH)
    dreamQ.connect('tcp://{}:{}'.format(host, IO.DREAM))


    stateQ, eventQ, brainQ = _three_amigos(context, host)

    sender = context.socket(zmq.PUSH)
    sender.connect('tcp://{}:{}'.format(host, IO.EXTERNAL))

    counterQ = context.socket(zmq.REQ)
    counterQ.connect('tcp://{}:{}'.format(host, IO.COUNTER))
    
    poller = zmq.Poller()
    poller.register(mic, zmq.POLLIN)
    poller.register(stateQ, zmq.POLLIN)
    poller.register(eventQ, zmq.POLLIN)

    audio = deque()
    NAPs = []
    wavs = []
    wav_audio_ids = {}
    NAP_hashes = {}

    # audio_classifier = []
    # audio_recognizer = []
    # global_audio_recognizer = []
    # mixture_audio_recognizer = []
    # maxlen = []

    deleted_ids = []
    
    state = stateQ.recv_json()
    
    black_list = open('black_list.txt', 'a')

    audio_memory = AudioMemory()
    
    if debug:
        import matplotlib.pyplot as plt
        plt.ion()

    while True:
        events = dict(poller.poll())
        
        if stateQ in events:
            state = stateQ.recv_json()

        if mic in events:
            new_audio = utils.recv_array(mic)
            if state['record']:
                audio.append(new_audio)

        if eventQ in events:
            pushbutton = eventQ.recv_json()
            if 'learn' in pushbutton:
                try:
                    t0 = time.time()
                    filename = pushbutton['filename']
                    audio_segment_times = utils.get_segments(filename)

                    print 'Learning {} duration {} seconds with {} segments'.format(filename, audio_segment_times[-1], len(audio_segment_times)-1)
                    new_sentence = utils.csv_to_array(filename + 'cochlear')
                    norm_segments = np.rint(new_sentence.shape[0]*audio_segment_times/audio_segment_times[-1]).astype('int')

                    audio_segments = []
                    new_audio_hash = []
                    amps = utils.get_amps(filename)
                    most_significant_value = -np.inf
                    most_significant_audio_id = []

                    original_NAP_length = len(NAPs)
                    
                    for segment, new_sound in enumerate([ utils.trim_right(new_sentence[norm_segments[i]:norm_segments[i+1]]) for i in range(len(norm_segments)-1) ]):
                        # We filter out short, abrupt sounds with lots of noise.
                        if np.mean(new_sound) < 2 or new_sound.shape[0] == 0:
                          black_list.write('{} {}\n'.format(filename, segment))
                          print 'BLACKLISTED segment {} in file {}'.format(segment, filename)
                          continue

                        if debug:
                            utils.plot_NAP_and_energy(new_sound, plt)

                        audio_segment = audio_memory.learn(new_sound, filename, audio_segment_times[segment], audio_segment_times[segment+1], audio_segment_times[-1])

                        audio_segments.append(audio_segment)
                        if amps[segment] > most_significant_value:
                            most_significant_audio_id = audio_segment.audio_id
                            most_significant_value = amps[segment]

                    black_list.flush()
                    print 'AUDIO IDs after blacklisting {}'. format([ audio_segment.audio_id for audio_segment in audio_segments ])
                    if len(audio_segments):
                        # if len(audio_memory.audio_ids.keys()) > AUDIO_MEMORY_SIZE:
                        #     counterQ.send_pyobj(['audio_ids_counter', None])
                        #     freqs = counterQ.recv_pyobj()
                        #     histogram = np.zeros(max(audio_memory.audio_ids.keys()))
                        #     for index in freqs.keys():
                        #         histogram[index] = freqs[index]
                        #     histogram[deleted_ids] = np.inf
                        #     protect = int(AUDIO_MEMORY_SIZE*PROTECT_PERCENTAGE)
                        #     histogram[-protect:] = np.inf
                        #     loner = np.where(histogram == min(histogram))[0][0]
                        #     filter(audio_memory.forget, audio_memory.audio_ids[loner])
                        #     print 'Forgetting audio_id {} from audio_memory, size after deletion: {}'.format(loner, len(audio_memory.audio_ids.keys()))
                        #     deleted_ids.append(loner)

                        # while len(NAPs) - len(deleted_ids) > AUDIO_MEMORY_SIZE:
                        #     utils.delete_loner(counterQ, NAPs, 'audio_ids_counter', int(AUDIO_MEMORY_SIZE*PROTECT_PERCENTAGE), deleted_ids)

                        # maxlen = max([ m.shape[0] for memory in NAPs for m in memory if len(m) ])
                        # memories = [ np.ndarray.flatten(utils.zero_pad(m, maxlen)) for memory in NAPs for m in memory if len(m) ]

                        # targets = [ i for i,f in enumerate(NAPs) for k in f if len(k) ]
                        # audio_classifier = train_rPCA_SVM(memories, targets)

                        # all_hammings = [ utils.hamming_distance(new_audio_hash[i], new_audio_hash[j])
                        #                                         for i in range(len(new_audio_hash)) for j in range(len(new_audio_hash)) if i > j ]
                    
                        # print 'RHYME VALUE', np.mean(sorted(all_hammings)[int(len(all_hammings)/2):])
                        # rhyme = np.mean(sorted(all_hammings)[int(len(all_hammings)/2):]) < RHYME_HAMMERTIME

                        # sender.send_json('rhyme {}'.format(rhyme))

                        brainQ.send_pyobj(['audio_learn', filename, audio_segments, audio_memory, most_significant_audio_id])
                        print 'Audio learned from {} in {} seconds'.format(filename, time.time() - t0)
                    else:
                        print 'SKIPPING fully blacklisted file {}'.format(filename)
                except:
                    utils.print_exception('Audio learning aborted.')

                audio.clear()

            if 'dream' in pushbutton:
                print dream(audio_memory)
                     
            if 'save' in pushbutton:
                utils.save('{}.{}'.format(pushbutton['save'], mp.current_process().name), [ deleted_ids, NAPs, wavs, wav_audio_ids, NAP_hashes, audio_memory ])
                
            if 'load' in pushbutton:
                deleted_ids, NAPs, wavs, wav_audio_ids, NAP_hashes, audio_memory = utils.load('{}.{}'.format(pushbutton['load'], mp.current_process().name))
                            

def dream(audio_memory):
    
    #import matplotlib.pyplot as plt
    #plt.ion()

    try:
        print 'Dreaming - removing wrongly binned filenames'

        for audio_id, audio_segments in audio_memory.audio_ids.iteritems():

            print 'Examining audio_id {}'.format(audio_id)
            if len(audio_segments) == 1:
                print 'Just one member in this audio_id, skipping analysis'
                continue


            NAP_detail = 'low'
            candidates = zip(audio_segments, [ NAP_detail ]*len(audio_segments))
                
            k = 2
            sparse_codes = mysai.experiment(candidates, k)
            # plt.matshow(sparse_codes, aspect='auto')
            # plt.colorbar()
            # plt.draw()

            coarse = np.mean(sparse_codes, axis=1)
            coarse.shape = (len(coarse), 1)

            codebook,_ = kmeans(coarse, k)
            instances = [ vq(np.atleast_2d(s), codebook)[0] for s in coarse ]

            freqs = itemfreq(instances)
            sorted_freqs = sorted(freqs, key=lambda x: x[1])
            print 'Average sparse codes: {} Class count: {}'.format(list(itertools.chain.from_iterable(coarse)), sorted_freqs)

            if len(sorted_freqs) == 1:
                print 'Considered to be all the same.'
                continue

            fewest_class = sorted_freqs[0][0]
            ousted_audio_segments = [ audio_segment for audio_segment, i in zip(audio_segments, instances) if i == fewest_class ]
            print 'Class {} has fewest members, deleting audio_segments {}'.format(fewest_class, ousted_audio_segments)
            filter(audio_memory.forget, ousted_audio_segments)

        print 'Creating mega super self-organized class'

        NAP_detail = 'high'
        all_segments = audio_memory.all_segments()
        candidates = zip(all_segments, [ NAP_detail ]*len(all_segments))

        high_resolution_k = 256
        clusters = 24
        sparse_codes = mysai.experiment(candidates, high_resolution_k)
        sparse_codes = np.array(sparse_codes)
        # plt.matshow(sparse_codes, aspect='auto')
        # plt.colorbar()
        # plt.draw()

        codebook,_ = kmeans(sparse_codes, clusters)
        instances = [ vq(np.atleast_2d(s), codebook)[0] for s in sparse_codes ]

        cluster_list = {}
        for audio_segment, instance in zip(all_segments, instances):
            utils.insert(cluster_list, instance, audio_segment)

        return cluster_list
    except:
        utils.print_exception('NIGHTMARE!')

                
            
def cognition(host):
    context = zmq.Context()

    eventQ = context.socket(zmq.SUB)
    eventQ.connect('tcp://{}:{}'.format(host, IO.EVENT))
    eventQ.setsockopt(zmq.SUBSCRIBE, b'') 

    stateQ = context.socket(zmq.SUB)
    stateQ.connect('tcp://{}:{}'.format(host, IO.STATE))
    stateQ.setsockopt(zmq.SUBSCRIBE, b'') 
    state = stateQ.recv_json()

    face = context.socket(zmq.SUB)
    face.connect('tcp://{}:{}'.format(host, IO.FACE))
    face.setsockopt(zmq.SUBSCRIBE, b'')

    association = context.socket(zmq.REQ)
    association.connect('tcp://{}:{}'.format(host, IO.ASSOCIATION))

    counterQ = context.socket(zmq.REQ)
    counterQ.connect('tcp://{}:{}'.format(host, IO.COUNTER))

    cognitionQ = context.socket(zmq.PULL)
    cognitionQ.bind('tcp://*:{}'.format(IO.COGNITION))

    sender = context.socket(zmq.PUSH)
    sender.connect('tcp://{}:{}'.format(host, IO.EXTERNAL))

    poller = zmq.Poller()
    poller.register(eventQ, zmq.POLLIN)
    poller.register(stateQ, zmq.POLLIN)
    poller.register(face, zmq.POLLIN)
    poller.register(cognitionQ, zmq.POLLIN)

    question = False
    rhyme = False
    rhyme_enable_once = True
    face_enable_once = True
    face_timer = 0
    minimum_face_interval = 40 # minimum time between face responses
    minimum_urge_to_say_something = 4
    default_minimum_urge_to_say_something = 4

    # (default) play events params
    default_voiceType1 = 1
    default_voiceType2 = 6
    default_wordSpace1 = 0.3
    default_wordSpaceDev1 = 0.3
    default_wordSpace2 = 0.1
    default_wordSpaceDev2 = 0.3

    lastSentenceIds = []
    last_most_significant_audio_id = 0
    face_recognizer = []
    last_face_id = []

    while True:
        events = dict(poller.poll())
        
        if cognitionQ in events:
            face_recognizer = cognitionQ.recv_pyobj()

        if face in events:
            new_face = utils.recv_array(face)
            if face_recognizer:
                this_face_id = [_predict_id(face_recognizer, new_face, counterQ, 'face_id')]
                if this_face_id != last_face_id:
                    print 'FACE ID', last_face_id
                    if time.time() - face_timer > minimum_face_interval:
                        face_timer = time.time()
                        face_enable_once = True
                        print 'face_enable_once', face_enable_once

                last_face_id = this_face_id

        if stateQ in events:
            state = stateQ.recv_json()

        if eventQ in events:
            pushbutton = eventQ.recv_json()
            
            if 'last_segment_ids' in pushbutton:
                lastSentenceIds = pushbutton['last_segment_ids']
                print 'LAST SENTENCE IDS', lastSentenceIds
            
            if 'last_most_significant_audio_id' in pushbutton:
                last_most_significant_audio_id = int(pushbutton['last_most_significant_audio_id'])
                print 'last_most_significant_audio_id learned', last_most_significant_audio_id

            if 'learn' in pushbutton or 'respond_sentence' in pushbutton:
                filename = pushbutton['filename']
                _,_,_,segmentData = utils.getSoundInfo(filename)
                pitches = [ item[3] for item in segmentData ]
                question = pitches[-1] > np.mean(pitches[:-1]) if len(pitches) > 1 else False
                print 'QUESTION ?', question
    
            if 'rhyme' in pushbutton:
                rhyme = pushbutton['rhyme']
                print 'RHYME ?', rhyme
                if rhyme:
                    rhyme_enable_once = True
                    minimum_urge_to_say_something = 0
                    sender.send_json('clear play_events')
                
            if 'urge_to_say_something' in pushbutton and (float(pushbutton['urge_to_say_something']) > minimum_urge_to_say_something) and state['enable_say_something']:
                print 'I feel the urge to say something...'
                print 'I can use, rhyme {} or face {} ..face_enable_once {}'.format(rhyme, last_face_id,face_enable_once)
                if rhyme and rhyme_enable_once:
                    rhyme_enable_once = False
                    print '*\n*I will now try to do a rhyme'
                    try:
                        rhyme_seed = last_most_significant_audio_id
                        print 'COGNITION IS WAITING...'
                        association.send_pyobj(['getSimilarWords',rhyme_seed, RHYME_HAMMERTIME])
                        rhymes = association.recv_pyobj()
                        print 'COGNITION OK'
                        if len(rhymes) > 7 : rhymes= rhymes[:7] # temporary length limit
                        print 'Rhyme sentence:', rhymes
                        sender.send_json('respond_setParam wordSpace 1 0')
                        sender.send_json('respond_setParam wordSpaceDev 1 0')
                        sender.send_json('play_sentence {}'.format(rhymes))
                        sender.send_json('respond_setParam wordSpace 1 {}'.format(default_wordSpace1))
                        sender.send_json('respond_setParam wordSpaceDev 1 {}'.format(default_wordSpaceDev1))
                        minimum_urge_to_say_something = default_minimum_urge_to_say_something
                    except:
                        utils.print_exception('Rhyme failed.')
                
                if len(last_face_id)>0  and face_enable_once and not rhyme: 
                    face_enable_once = False
                    print '*\n* I will do a face response on face {}'.format(last_face_id[0])
                    try:
                        association.send_pyobj(['getFaceResponse',last_face_id[0]])
                        face_response = association.recv_pyobj()
                        sender.send_json('play_sentence {}'.format([face_response]))
                    except:
                        utils.print_exception('Face response failed.')
                
                 
# LOOK AT EYES? CAN YOU DETERMINE ANYTHING FROM THEM?
# PRESENT VISUAL INFORMATION - MOVE UP OR DOWN
def people_detection(host, extended_search, people_detect, show):
    import cv2
    window_name = 'Visual detection'

    eye_cascade = cv2.cv.Load(EYE_HAAR_CASCADE_PATH)
    face_cascade = cv2.cv.Load(FACE_HAAR_CASCADE_PATH)
    storage = cv2.cv.CreateMemStorage()

    context = zmq.Context()
    camera = context.socket(zmq.SUB)
    camera.connect('tcp://{}:{}'.format(host, IO.CAMERA))
    camera.setsockopt(zmq.SUBSCRIBE, b'')

    publisher = context.socket(zmq.PUB)
    publisher.bind('tcp://*:{}'.format(IO.FACE))

    robocontrol = context.socket(zmq.PUSH)
    robocontrol.connect('tcp://localhost:{}'.format(IO.ROBO))

    if show:
        cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)

    face_detection_frame = 2
    motor_command_frame = 1
    i,j = [0,0]

    # We track object with constant velocity (assume people don't accelerate), measure just position
    transition_matrix=[[1,0,1,0],[0,1,0,1],[0,0,1,0],[0,0,0,1]]
    observation_matrix=[[1,0,0,0],[0,1,0,0]]

    init_frame = utils.recv_array(camera).copy()
    init_state = [init_frame.shape[1]/2, init_frame.shape[0]/2, 0, 0] # Dead center, no velocity
    init_cov = 1.0e-3*np.eye(4)
    transistion_cov = 1.0e-4*np.eye(4)
    observation_cov = 1.0e-1*np.eye(2)

    kf=KalmanFilter(transition_matrices=transition_matrix,
            observation_matrices=observation_matrix,
            initial_state_mean=init_state,
            initial_state_covariance=init_cov,
            transition_covariance=transistion_cov,
            observation_covariance=observation_cov)

    no_faces = np.ma.array([0,0])
    no_faces.mask = np.ma.masked
    # Bootstrapping the Kalman filter as if there was someone right in front of self.
    means, covs = kf.filter(np.tile(init_state[:2], [2,1])) 
    means = means[-1]
    covs = covs[-1]
    while True:
        frame = utils.recv_array(camera).copy() # Weird, but necessary to do a copy.

        if j%face_detection_frame == 0: 
            j = 0
        else:
            j += 1
            continue

        rows = frame.shape[1]
        cols = frame.shape[0]

        faces = [ (x,y,w,h) for (x,y,w,h),n in 
                  cv2.cv.HaarDetectObjects(cv2.cv.fromarray(frame), face_cascade, storage, 1.2, 2, cv2.cv.CV_HAAR_DO_CANNY_PRUNING, (50,50)) ] 

        if extended_search:
            eyes = [ (x,y,w,h) for (x,y,w,h),n in 
                     cv2.cv.HaarDetectObjects(cv2.cv.fromarray(frame), eye_cascade, storage, 1.2, 2, cv2.cv.CV_HAAR_DO_CANNY_PRUNING, (20,20)) ] 

            try: 
                if len(eyes) == 2:
                    x, y, _, _ = eyes[0]
                    x_, y_, _, _ = eyes[1]
                    angle = np.rad2deg(np.arctan( float((y_ - y))/(x_ - x) ))
                    rotation = cv2.getRotationMatrix2D((rows/2, cols/2), angle, 1)
                    frame = cv2.warpAffine(frame, rotation, (rows,cols))

                    faces.extend([ (x,y,w,h) for (x,y,w,h),n in 
                                   cv2.cv.HaarDetectObjects(cv2.cv.fromarray(frame), face_cascade, storage, 1.2, 2, cv2.cv.CV_HAAR_DO_CANNY_PRUNING, (50,50)) ])
            except Exception, e:
                print e, 'Eye detection failed.'

        # We select the biggest face.
        if faces:
            faces_sorted = sorted(faces, key=lambda x: x[2]*x[3], reverse=True)
            x,y,w,h = faces_sorted[0]
            x_diff = (rows/2. - (x + w/2.))/rows
            y_diff = (y + h/2. - cols/2.)/cols
            resized_face = cv2.resize(frame[y:y+h, x:x+w], (100,100))
            gray_face = cv2.cvtColor(resized_face, cv2.COLOR_BGR2GRAY) / 255.
            
            utils.send_array(publisher, gray_face)
            i += 1
            if i%motor_command_frame == 0:
                if abs(x_diff) > .1:
                    robocontrol.send_json([ 1, 'pan', .25*np.sign(x_diff)*x_diff**2]) 
                robocontrol.send_json([ 1, 'tilt', .5*np.sign(y_diff)*y_diff**2])
                i = 0
                
        # People detection
        found_filtered = []
        if people_detect:
            found = list(cv2.cv.HOGDetectMultiScale(cv2.cv.fromarray(frame), storage, win_stride=(8,8), padding=(32,32), scale=1.05, group_threshold=2))
            for r in found:
                insidef = False
                for q in found:
                    if utils.inside(r, q):
                        insidef = True
                        break
                if not insidef:
                    found_filtered.append(r)

        if show:
            # Show Kalman filter prediction
            measurement = [ x + w/2, y + h/2 ] if faces else no_faces
            means, covs = kf.filter_update(means, covs, measurement)
            center = tuple(map(int, means[:2]))
            radius = 10
            cv2.circle(frame, center, radius, (255,255,255))
            # Print covariance ellipse
            # covar = np.array(covs)
            # covar = covar[:2,:2]
            # v, w = np.linalg.eigh(covar)
            # u = w[0] / np.linalg.norm(w[0])
            # angle = np.arctan(u[1] / u[0])
            # angle = 180 * angle / np.pi
            # chisquare_val = 2.4477
            # halfmajoraxissize=chisquare_val*np.sqrt(v[0])
            # halfminoraxissize=chisquare_val*np.sqrt(v[1])
            # rotated_rect = 
            # cv2.ellipse(frame, center, 

            for r in found_filtered:
                (rx, ry), (rw, rh) = r
                tl = (rx + int(rw*0.1), ry + int(rh*0.07))
                br = (rx + int(rw*0.9), ry + int(rh*0.87))
                cv2.rectangle(frame, tl, br, (0, 255, 0), 3)

            if faces:
                cv2.rectangle(frame, (x,y), (x+w,y+h), (0, 0, 255), 2)
                cv2.line(frame, (x + w/2, y + h/2), (rows/2, cols/2), (255,0,0), 2)
                frame[-100:,-100:] = resized_face
                cv2.waitKey(1)

            cv2.imshow(window_name, frame)


def train_network(x, y, output_dim=100, leak_rate=.9, bias_scaling=.2, reset_states=True, use_pinv=True):
    import Oger
    import mdp

    mdp.numx.random.seed(7)

    reservoir = Oger.nodes.LeakyReservoirNode(output_dim=output_dim, 
                                              leak_rate=leak_rate, 
                                              bias_scaling=bias_scaling, 
                                              reset_states=reset_states)
    readout = mdp.nodes.LinearRegressionNode(use_pinv=use_pinv)
        
    net = mdp.hinet.FlowNode(reservoir + readout)
    net.train(x,y)

    return net

                
def cochlear(filename, stride, rate, db=-40, ears=1, a_1=-0.995, apply_filter=1, suffix='cochlear'):
    original_rate, data = wavfile.read(filename)
    assert data.dtype == np.int16
    data = data / float(2**15)
    if original_rate != rate:
        data = resample(data, float(rate)/original_rate, 'sinc_best')
    data = data*10**(db/20)
    utils.array_to_csv('{}-audio.txt'.format(filename), data)
    call(['./carfac-cmd', filename, str(len(data)), str(ears), str(rate), str(stride), str(a_1), str(apply_filter), suffix])
    naps = utils.csv_to_array(filename+suffix)
    return np.sqrt(np.maximum(0, naps)/np.max(naps))


def _predict_id(classifier, M, counterQ = False, modality = None):
    x_test = classifier.rPCA.transform(np.ndarray.flatten(M))
    _id = classifier.predict(x_test)[0]
    if counterQ: # This happens insanely fast.
        counterQ.send_pyobj([modality, _id])
        counterQ.recv_pyobj()
    return _id

def _hamming_distance_predictor(audio_classifier, NAP, maxlen, NAP_hashes):
    NAP_hash = utils.d_hash(NAP, hash_size=8)
    NAP_scales = [ utils.exact(NAP, maxlen), 
                   utils.exact(resample(NAP, .5, 'sinc_best'), maxlen), 
                   utils.exact(resample(NAP, min(2, float(maxlen)/NAP.shape[0]), 'sinc_best'), maxlen) ]

    audio_id_candidates = [ _predict_id(audio_classifier, NAP_s) for NAP_s in NAP_scales ]
    hamming_warped = [ np.mean([ utils.hamming_distance(NAP_hash, h) for h in NAP_hashes[audio_id] ]) for audio_id in audio_id_candidates ]
    print 'HAMMING WARPED', zip(audio_id_candidates, hamming_warped)
    winner = np.argsort(hamming_warped)[0]
    return audio_id_candidates[winner], NAP_scales[winner]

def _recognize_audio_id(audio_recognizer, NAP):
    for audio_id, net in enumerate(audio_recognizer):
        print 'AUDIO ID: {} OUTPUT MEAN: {}'.format(audio_id, np.mean(net(NAP)))

def _extract_NAP(audio_segment, suffix='cochlear'):
    NAP = utils.csv_to_array(audio_segment.wav_file + suffix)
    return utils.trim_right(NAP[ int(NAP.shape[0]*audio_segment.normalized_start) : int(NAP.shape[0]*audio_segment.normalized_end) ])
                    
def _train_audio_recognizer(signal):
    noise = np.random.rand(signal.shape[0], signal.shape[1])
    x_train = np.vstack((noise, signal))
    targets = np.array([-1]*noise.shape[0] + [1]*signal.shape[0])
    targets.shape = (len(targets), 1)
    return train_network(x_train, targets, output_dim=100, leak_rate=.1)

def _train_mixture_audio_recognizer(NAPs):
    x_train = np.vstack([m for memory in NAPs for m in memory])
    nets = []
    for audio_id, _ in enumerate(NAPs):
        targets = np.array([ 1 if i == audio_id else -1 for i,nappers in enumerate(NAPs) for nap in nappers for _ in range(nap.shape[0]) ])
        targets.shape = (len(targets),1)
        nets.append(train_network(x_train, targets, output_dim=10, leak_rate=.5))
    return nets

def _train_global_audio_recognizer(NAPs):
    x_train = np.vstack([m for memory in NAPs for m in memory])
    targets = np.zeros((x_train.shape[0], len(NAPs))) - 1
    idxs = [ i for i,nappers in enumerate(NAPs) for nap in nappers for _ in range(nap.shape[0]) ]
    for row, ix in zip(targets, idxs):
        row[ix] = 1
    return train_network(x_train, targets, output_dim=1000, leak_rate=.9)
    
def _recognize_audio_id(audio_recognizer, NAP):
    for audio_id, net in enumerate(audio_recognizer):
        print 'AUDIO ID: {} OUTPUT MEAN: {}'.format(audio_id, np.mean(net(NAP)))

def _recognize_mixture_audio_id(audio_recognizer, NAP):
    print 'MIXTURE AUDIO IDS:', np.mean(np.hstack([ net(NAP) for net in audio_recognizer ]), axis=0)

def _recognize_global_audio_id(audio_recognizer, NAP, plt):
    output = audio_recognizer(NAP)
    plt.clf()
    plt.subplot(211)
    plt.plot(output)
    plt.xlim(xmax=len(NAP))
    plt.legend([ str(i) for i,_ in enumerate(output) ])
    plt.title('Recognizers')

    plt.subplot(212)
    plt.imshow(NAP.T, aspect='auto')
    plt.title('NAP')
    
    plt.draw()

    print 'GLOBAL AUDIO IDS:', np.mean(output, axis=0)

class OneTrickPony:
    def predict(self, x):
        return np.array([0])
    
def train_rPCA_SVM(x, y):
    rPCA = RandomizedPCA(n_components=100)
    x = rPCA.fit_transform(x)

    if len(np.unique(y)) == 1:
        classifier = OneTrickPony()
        classifier.rPCA = rPCA
    else:
        classifier = svm.LinearSVC()
        classifier.fit(x, y)
        classifier.rPCA = rPCA

    return classifier
    
                
def learn_video(host, debug=False):
    import cv2

    context = zmq.Context()

    camera = context.socket(zmq.SUB)
    camera.connect('tcp://{}:{}'.format(host, IO.CAMERA))
    camera.setsockopt(zmq.SUBSCRIBE, b'')

    stateQ, eventQ, brainQ = _three_amigos(context, host)
    
    poller = zmq.Poller()
    poller.register(camera, zmq.POLLIN)
    poller.register(stateQ, zmq.POLLIN)
    poller.register(eventQ, zmq.POLLIN)

    video = deque()

    state = stateQ.recv_json()

    while True:
        events = dict(poller.poll())
        
        if stateQ in events:
            state = stateQ.recv_json()
             
        if camera in events:
            new_video = utils.recv_array(camera)
            if state['record']:
                frame = cv2.resize(new_video, FRAME_SIZE)
                gray_image = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) / 255.
                gray_flattened = np.ndarray.flatten(gray_image)
                video.append(gray_flattened)

        if eventQ in events:
            pushbutton = eventQ.recv_json()
            if 'learn' in pushbutton:
                try:
                    t0 = time.time()
                    filename = pushbutton['filename']
                    new_sentence = utils.trim_right(utils.csv_to_array(filename + 'cochlear'))
                    
                    video_segment = np.array(list(video))
                    if video_segment.shape[0] == 0:
                        video_segment = np.array([ np.ndarray.flatten(np.zeros(FRAME_SIZE)) for _ in range(10) ])
                        print 'No video recorded. Using black image as stand-in.'

                    stride = IO.VIDEO_SAMPLE_TIME / (IO.NAP_RATE/IO.NAP_STRIDE)
                    x = new_sentence[::stride]
                    min_length = min(x.shape[0], video_segment.shape[0])
                    x = new_sentence[:min_length]
                    y = video_segment[:min_length]

                    tarantino = train_network(x,y, output_dim=10)

                    esn_name = '{}video_esn_{}'.format(myCsoundAudioOptions.memRecPath, uuid4())

                    utils.dump_esn(tarantino, esn_name)
                    
                    brainQ.send_pyobj([ 'video_learn', filename, esn_name ])
                    print 'Video related to {} learned in {}'.format(filename, time.time() - t0)
                except:
                    utils.print_exception('Video learning aborted.')

                video.clear()

                
def learn_faces(host, debug=False):
    context = zmq.Context()

    face = context.socket(zmq.SUB)
    face.connect('tcp://{}:{}'.format(host, IO.FACE))
    face.setsockopt(zmq.SUBSCRIBE, b'')

    stateQ, eventQ, brainQ = _three_amigos(context, host)

    counterQ = context.socket(zmq.REQ)
    counterQ.connect('tcp://{}:{}'.format(host, IO.COUNTER))
    
    poller = zmq.Poller()
    poller.register(face, zmq.POLLIN)
    poller.register(stateQ, zmq.POLLIN)
    poller.register(eventQ, zmq.POLLIN)

    faces = deque()
    face_history = []
    face_hashes = []
    face_recognizer = []

    state = stateQ.recv_json()

    deleted_ids = []
    
    while True:
        events = dict(poller.poll())
        
        if stateQ in events:
            state = stateQ.recv_json()
        
        if face in events:
            new_face = utils.recv_array(face)

            if state['record']:
                faces.append(new_face)

        if eventQ in events:
            pushbutton = eventQ.recv_json()
            if 'learn' in pushbutton:
                try:
                    t0 = time.time()
                    filename = pushbutton['filename']

                    # Do we know this face?
                    hammings = [ np.inf ]
                    new_faces = list(faces)
                    new_faces_hashes = [ utils.d_hash(f) for f in new_faces ]

                    face_id = -1
                    if new_faces:
                        face_id = 0
                        
                        if face_recognizer:
                            predicted_faces = [ _predict_id(face_recognizer, f) for f in new_faces ]
                            uniq = np.unique(predicted_faces)
                            face_id = uniq[np.argsort([ sum(predicted_faces == u) for u in uniq ])[-1]]
                            hammings = [ utils.hamming_distance(f, m) for f in new_faces_hashes for m in face_hashes[face_id] ]

                        if np.mean(hammings) < FACE_HAMMERTIME:
                            while len(face_history[face_id]) > MAX_CATEGORY_SIZE:
                                face_history[face_id].pop(0)
                                face_hashes[face_id].pop(0)
    
                            face_history[face_id].append(new_faces[-1]) 
                            face_hashes[face_id].append(new_faces_hashes[-1])
                            print 'Face is similar to face {}, hamming mean {}'.format(face_id, np.mean(hammings))
                        else:
                            print 'New face, hamming mean {} from face {}'.format(np.mean(hammings), face_id)
                            face_history.append([new_faces[-1]])
                            face_hashes.append([new_faces_hashes[-1]])
                            face_id = len(face_history) - 1

                        while len(face_history) - len(deleted_ids) > FACE_MEMORY_SIZE:
                            utils.delete_loner(counterQ, face_history, 'face_ids_counter', int(FACE_MEMORY_SIZE*PROTECT_PERCENTAGE), deleted_ids)

                        x_train = [ np.ndarray.flatten(f) for cluster in face_history for f in cluster if len(f) ]
                        targets = [ i for i,cluster in enumerate(face_history) for f in cluster if len(f) ]

                        face_recognizer = train_rPCA_SVM(x_train, targets)
                    else:
                        print 'Face not detected.'

                    brainQ.send_pyobj([ 'face_learn', filename, face_id, face_recognizer ])
                    print 'Faces related to {} learned in {} seconds'.format(filename, time.time() - t0)
                except:
                    utils.print_exception('Face learning aborted.')

                faces.clear()

            if 'save' in pushbutton:
                utils.save('{}.{}'.format(pushbutton['save'], mp.current_process().name), [ deleted_ids, face_history, face_hashes, face_recognizer ])

            if 'load' in pushbutton:
                deleted_ids, face_history, face_hashes, face_recognizer = utils.load('{}.{}'.format(pushbutton['load'], mp.current_process().name))


def _three_amigos(context, host):
    stateQ = context.socket(zmq.SUB)
    stateQ.connect('tcp://{}:{}'.format(host, IO.STATE))
    stateQ.setsockopt(zmq.SUBSCRIBE, b'') 

    eventQ = context.socket(zmq.SUB)
    eventQ.connect('tcp://{}:{}'.format(host, IO.EVENT))
    eventQ.setsockopt(zmq.SUBSCRIBE, b'') 

    brainQ = context.socket(zmq.PUSH)
    brainQ.connect('tcp://{}:{}'.format(host, IO.BRAIN))

    return stateQ, eventQ, brainQ
