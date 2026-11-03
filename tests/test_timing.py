"""Sequence mapper and gate behaviour (ported from the frozen v0.2 experiment tests)."""
import unittest
from bazarr_sync.timing import SeqConfig, map_cues, tokens, align, gate, window_stream

def speak(text,start,step=.3,smoothing=0):
    out=[];t=start
    for w in text.split():out.append(dict(text=w,start_time=t,end_time=t+step*.8,smoothing_seconds=smoothing));t+=step
    return out
def cue(i,text,start,end):return dict(cue_index=i,text=text,start_seconds=start,end_seconds=end)
CFG=SeqConfig(caption_lead_seconds=0,caption_trail_seconds=0)

class Tokenize(unittest.TestCase):
    def test_markup_sfx_apostrophes_accents(self):
        self.assertEqual(tokens("<i>- Isn't it</i> [door slams] Sí, walk-in!"),['isnt','it','si','walk','in'])

class Align(unittest.TestCase):
    def test_compound_merge_both_directions(self):
        sub=['walk','in','now'];asr=['walkin','now']
        kinds=[k for *_,k in align(sub,[0,.1,.2],asr,[0,.2],CFG)]
        self.assertEqual(kinds,['merge','exact'])
        kinds=[k for *_,k in align(['walkin'],[0],['walk','in'],[0,.1],CFG)]
        self.assertEqual(kinds,['merge'])

class Mapping(unittest.TestCase):
    def test_uniform_shift_recovered_with_asr_errors(self):
        cues=[cue(0,'the silver lantern stood beside the gate',10,12),cue(1,'martha folded the blue map carefully',13,15)]
        words=speak('the silver lanterns stood beside a gate',13)+speak('martha folded the blue map carefully',16)
        rows=map_cues(cues,words,CFG)
        self.assertTrue(all(r['status']=='aligned'for r in rows))
        self.assertAlmostEqual(rows[0]['start_seconds'],13,places=6)
        self.assertAlmostEqual(rows[1]['start_seconds'],16,places=6)

    def test_short_cue_between_anchors(self):
        cues=[cue(0,'bring the bottom tray from the walk-in',10,12),cue(1,'Gracias.',13,13.5),cue(2,'now clean the whole station please',14,16)]
        words=speak('bring the bottom tray from the walkin',12)+speak('gracias',15.2)+speak('now clean the whole station please',16.5)
        rows=map_cues(cues,words,CFG)
        self.assertEqual([r['status']for r in rows],['aligned']*3)
        self.assertAlmostEqual(rows[1]['start_seconds'],15.2,places=6)

    def test_repeated_short_cue_abstains(self):
        cues=[cue(0,'we need to finish the stock today',10,12),cue(1,'Yeah.',13,13.3),cue(2,'and then we close the kitchen early',14,16)]
        words=speak('we need to finish the stock today',10)+speak('yeah yeah',12.5)+speak('and then we close the kitchen early',14)
        rows=map_cues(cues,words,CFG)
        self.assertEqual(rows[1]['status'],'unresolved');self.assertEqual(rows[1]['reason'],'repeated_phrase_ambiguous_in_context')

    def test_unclaimed_repeat_of_long_cue_abstains(self):
        rows=map_cues([cue(0,'the silver lantern stood beside the gate',6,8)],speak('the silver lantern stood beside the gate',3)+speak('the silver lantern stood beside the gate',9),CFG)
        self.assertEqual(rows[0]['reason'],'repeated_phrase_ambiguous')

    def test_repeat_claimed_by_other_cue_is_fine(self):
        cues=[cue(0,'the silver lantern stood beside the gate',3,5),cue(1,'the silver lantern stood beside the gate',9,11)]
        rows=map_cues(cues,speak('the silver lantern stood beside the gate',3.5)+speak('the silver lantern stood beside the gate',9.5),CFG)
        self.assertEqual([r['status']for r in rows],['aligned','aligned'])

    def test_short_cue_needs_two_neighbours(self):
        rows=map_cues([cue(0,'Gracias.',1,1.5),cue(1,'now clean the whole station please',3,5)],speak('gracias',1)+speak('now clean the whole station please',3),CFG)
        self.assertEqual(rows[0]['reason'],'short_cue_without_context')

    def test_absent_dialogue_and_sfx_keep_source(self):
        cues=[cue(0,'the silver lantern stood beside the gate',10,12),cue(1,'completely different words nobody said here',13,15),cue(2,'[thunder]',16,17)]
        rows=map_cues(cues,speak('the silver lantern stood beside the gate',10.5),CFG)
        self.assertEqual(rows[0]['status'],'aligned')
        self.assertEqual((rows[1]['status'],rows[1]['start_seconds'],rows[1]['end_seconds']),('unresolved',13,15))
        self.assertEqual(rows[2]['reason'],'no_spoken_text')

    def test_unreliable_endpoint_word_preserves_duration(self):
        words=speak('the silver lantern stood beside the gate',11)
        words[0]['smoothing_seconds']=1.0
        rows=map_cues([cue(0,'the silver lantern stood beside the gate',10,13)],words,CFG)
        self.assertEqual(rows[0]['method'],'end_direct')
        self.assertAlmostEqual(rows[0]['end_seconds']-rows[0]['start_seconds'],3,places=6)

    def test_large_change_rejected(self):
        rows=map_cues([cue(0,'the silver lantern stood beside the gate',10,12)],speak('the silver lantern stood beside the gate',22),SeqConfig(band_seconds=20))
        self.assertEqual(rows[0]['reason'],'large_change_requires_review')

    def test_wording_never_returned_and_order_kept(self):
        cues=[cue(i,f'line number {w} spoken clearly',i*3,i*3+2)for i,w in enumerate(['one','two','three'])]
        rows=map_cues(cues,[w for i,x in enumerate(['one','two','three'])for w in speak(f'line number {x} spoken clearly',i*3+1)],CFG)
        self.assertEqual([r['cue_index']for r in rows],[0,1,2]);self.assertTrue(all('text'not in r for r in rows))

class Gate(unittest.TestCase):
    def rows(self,shifts):
        cues=[cue(i,'x',i*3,i*3+2)for i in range(len(shifts))]
        rows=[dict(cue_index=i,start_seconds=i*3+s,end_seconds=i*3+2+s,status='aligned')if s is not None else
              dict(cue_index=i,start_seconds=i*3,end_seconds=i*3+2,status='unresolved')for i,s in enumerate(shifts)]
        return cues,rows

    def test_outlier_direct_rejected_against_prior(self):
        cues,rows=self.rows([0,0,-5.6,0,0])
        keep,prior,dec=gate(cues,rows,[c['start_seconds']for c in cues])
        self.assertEqual(keep[2]['reason'],'direct_disagrees_with_section');self.assertEqual(prior,[0]*5)

    def test_prior_overridden_when_anchors_agree(self):
        cues,rows=self.rows([0,0,0,0,None,0,0])
        keep,prior,dec=gate(cues,rows,[c['start_seconds']-3 for c in cues])
        self.assertEqual(prior,[0]*7);self.assertTrue(dec[4].startswith('prior_overridden'))

    def test_prior_kept_at_disagreeing_transition(self):
        cues,rows=self.rows([4,4,4,None,2,2,2])
        keep,prior,dec=gate(cues,rows,[c['start_seconds']+(4 if i<3 else 2)for i,c in enumerate(cues)],SeqConfig(gate_neighbour_cues=3))
        self.assertEqual(prior[3],2);self.assertEqual(dec[3],'prior')

class Stream(unittest.TestCase):
    def test_overlapping_windows_keep_each_word_once_from_central_window(self):
        word=lambda t,a:dict(text=t,start_time=a,end_time=a+.2)
        wins=[dict(start_seconds=0,duration_seconds=30,words=[word('a',5),word('b',18),word('c',25)]),
              dict(start_seconds=10,duration_seconds=30,words=[word('b',8),word('c',15),word('d',25)])]
        self.assertEqual([(w['text'],round(w['start_time'],3)) for w in window_stream(wins)],[('a',5),('b',18),('c',25),('d',35)])

if __name__=='__main__':unittest.main()
