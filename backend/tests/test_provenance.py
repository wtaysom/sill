import pytest
import provenance as p


def test_production_is_optional():
    assert p.production_from_value(None, session=None, as_=None, when='W') is None
    assert p.render(None, model_for=lambda e: None) == ['made: undeclared']


def test_generic_people_models_and_voices():
    value = p.production_from_value('Inez: experiment; model-7 as Field Guide: analysis', session='s1', as_=None, when='W')
    assert value['made'][0]['who'] == 'Inez'
    assert value['made'][1]['as'] == 'Field Guide'
    assert 'Field Guide' in ' '.join(p.render(value, model_for=lambda e: None))
    with pytest.raises(ValueError, match='model id|precise'):
        p.production_from_value('gpt: analysis', session='s1', as_=None, when='W')
    with pytest.raises(ValueError, match='made-json|object'):
        p.production_from_value('self: analysis', session=None, as_=None, when='W')


def test_explicit_person_can_share_a_model_family_name():
    value = {'made': [{'kind':'person', 'who':'Claude', 'work':'experiment', 'at':[],
                      'declared':{'by':None, 'when':'W', 'basis':'stated'}}]}
    p.validate(value)


def test_compact_grammar_preserves_addresses_and_declarer():
    value = p.production_from_value('self as Surveyor: "analysis; comparison" @ "file:a,b" [self-report]; session=host:old as Reader: notes [stated]',
                                    session='host:new', as_=None, when='W')
    first, second = value['made']
    assert first['who'] is None and first['session'] == 'host:new'
    assert first['declared']['by']['as'] == 'Surveyor'
    assert first['at'] == ['file:a,b'] and first['work'] == 'analysis; comparison'
    assert second['session'] == 'host:old'
    assert 'model unknown' in ' '.join(p.render(value, model_for=lambda e: None, detailed=True))


@pytest.mark.parametrize('text', ['Inez: notes [witness]', 'Inez: notes [testimony]',
                                 'self: work [self-report from Inez] trailing', 'Inez: "unclosed'])
def test_invalid_declarations_fail(text):
    with pytest.raises(p.ProvenanceError):
        p.production_from_value(text, session='synthetic', as_=None, when='W')


def test_invalid_session_type_fails_validation():
    with pytest.raises(p.ProvenanceError, match='session'):
        p.validate({'made':[{'kind':'model','who':None,'session':123,'work':'notes','at':[],
                            'declared':{'by':None,'when':'W','basis':'stated'}}]})
